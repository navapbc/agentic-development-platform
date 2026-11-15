from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.repositories import GITHUB, LOCAL
from services.integration.tenant_config import Bundle, BundleIdentity
from services.orchestration.webhook.jira_events import (
    TemporalIssueIntake,
    TenantIssueEvent,
    TenantJiraIntakeHandler,
    issue_intake_workflow_id,
)
from services.orchestration.webhook.pr_events import (
    PostgresWorkflowResolver,
    PrClosedEvent,
    PrClosedHandler,
)
from services.orchestration.webhook.server import (
    build_routes_for,
    require_webhook_secrets,
    route_inbound,
)
from services.orchestration.webhook.tenants import (
    RoutedRepositories,
    TenantRouting,
    tenant_secrets,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.support import hermetic_settings

WEBHOOK = "webhook"
ADP_GITHUB_KEY = "adp-github-key"
BETA_GITHUB_KEY = "beta-github-key"
ADP_JIRA_KEY = "adp-jira-key"
BETA_JIRA_KEY = "beta-jira-key"
ENVIRON = {
    "GITHUB_WEBHOOK_SECRET_ADP": ADP_GITHUB_KEY,
    "GITHUB_WEBHOOK_SECRET_BETA": BETA_GITHUB_KEY,
    "JIRA_WEBHOOK_SECRET_ADP": ADP_JIRA_KEY,
    "JIRA_WEBHOOK_SECRET_BETA": BETA_JIRA_KEY,
}


@dataclass
class _Registry:
    repositories: dict[str, str]

    def names(self, backend: str) -> list[str]:
        return [name for name, kind in self.repositories.items() if kind == backend]


@dataclass
class _Registries:
    by_tenant: dict[str, _Registry]

    def tenants(self) -> list[str]:
        return list(self.by_tenant)

    def registry(self, tenant: str) -> _Registry | None:
        return self.by_tenant.get(tenant)


@dataclass
class _Source:
    files: dict[str, str]

    def latest(self) -> BundleIdentity:
        versions = {name: hashlib.sha256(t.encode()).hexdigest() for name, t in self.files.items()}
        return BundleIdentity("t", "directory", versions)

    def fetch(self, identity: BundleIdentity) -> Bundle:
        return Bundle(identity, dict(self.files))


def _jira_yaml(*projects: str) -> str:
    return json.dumps({"config_version": 99, "allowlist": list(projects)})


def _routing(
    by_tenant: dict[str, dict[str, str]],
    jira: dict[str, str] | None = None,
    *,
    settings: Any = None,
) -> TenantRouting:
    sources = {tenant: _Source({"jira": text}) for tenant, text in (jira or {}).items()}
    return TenantRouting(
        settings or hermetic_settings(tenants=",".join(by_tenant)),
        registries=cast(Any, _Registries({t: _Registry(repos) for t, repos in by_tenant.items()})),
        source_factory=lambda tenant: cast(Any, sources.get(tenant, _Source({}))),
    )


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_body(repo: str, task_id: str = "T-7") -> bytes:
    return json.dumps(
        {
            "action": "closed",
            "repository": {"name": repo},
            "pull_request": {"number": 3, "merged": True, "head": {"ref": f"agent/{task_id}"}},
        }
    ).encode()


def _jira_body(key: str) -> bytes:
    return json.dumps(
        {
            "webhookEvent": "jira:issue_created",
            "timestamp": 1700000000,
            "issue": {"id": "10001", "key": key, "fields": {"project": {"key": "SPOOF"}}},
        }
    ).encode()


@dataclass
class _Resolver:
    events: list[PrClosedEvent] = field(default_factory=list)

    async def resolve(self, event: PrClosedEvent) -> str | None:
        self.events.append(event)
        return f"wf-{event.tenant}-{event.task_id}"


@dataclass
class _Signaller:
    signals: list[str] = field(default_factory=list)

    async def signal_pr_closed(self, workflow_id: str, *, merged: bool) -> None:
        self.signals.append(workflow_id)


@dataclass
class _Starter:
    events: list[TenantIssueEvent] = field(default_factory=list)

    async def start(self, event: TenantIssueEvent) -> dict[str, Any]:
        self.events.append(event)
        return {"status": "started", "workflow_id": issue_intake_workflow_id(event)}


def _two_tenant_routes(monkeypatch, resolver: _Resolver, starter: _Starter):
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    settings = hermetic_settings(tenants="adp,beta", tenant="adp")
    routing = _routing(
        {"adp": {"adp-web": GITHUB, "adp-local": LOCAL}, "beta": {"beta-web": GITHUB}},
        {"adp": _jira_yaml("ADP"), "beta": _jira_yaml("BETA")},
        settings=settings,
    )
    return build_routes_for(
        settings, routing=routing, signaller=_Signaller(), starter=starter, resolver=resolver
    )


async def _post(route, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return await route_inbound(
        route, body, headers, ledger=InMemorySideEffectLedger(), mode=WEBHOOK
    )


def test_each_github_repository_routes_to_the_tenant_that_lists_it():
    routing = _routing(
        {"adp": {"adp-web": GITHUB, "adp-local": LOCAL}, "beta": {"beta-web": GITHUB}}
    )

    assert routing.repository_owners() == {"adp-web": "adp", "beta-web": "beta"}
    assert routing.github_repositories() == frozenset({"adp-web", "beta-web"})


def test_a_repository_two_tenants_list_is_refused():
    routing = _routing({"adp": {"shared": GITHUB}, "beta": {"shared": GITHUB}})

    assert routing.owner("shared") is None
    assert "shared" not in routing.github_repositories()


def test_a_repository_claimed_later_stays_with_its_tenant():
    registries = _Registries({"adp": _Registry({"shared": GITHUB}), "beta": _Registry({})})
    routing = TenantRouting(hermetic_settings(tenants="adp,beta"), registries=cast(Any, registries))
    assert routing.owner("shared") == "adp"

    registries.by_tenant["beta"] = _Registry({"shared": GITHUB})

    assert routing.owner("shared") == "adp"


def test_a_tenant_secret_is_its_own_and_the_platform_secret_serves_one_tenant_only():
    assert tenant_secrets(["adp", "beta"], "GITHUB_WEBHOOK_SECRET_", "shared", ENVIRON) == {
        "adp": ADP_GITHUB_KEY,
        "beta": BETA_GITHUB_KEY,
    }
    assert tenant_secrets(["adp", "beta"], "GITHUB_WEBHOOK_SECRET_", "shared", {}) == {}
    assert tenant_secrets(["adp"], "GITHUB_WEBHOOK_SECRET_", "shared", {}) == {"adp": "shared"}
    assert tenant_secrets([""], "GITHUB_WEBHOOK_SECRET_", "shared", ENVIRON) == {"": "shared"}


def test_a_pr_delivery_verifies_with_the_secret_of_the_repository_owner():
    routing = _routing({"adp": {"adp-web": GITHUB}, "beta": {"beta-web": GITHUB}})
    handler = PrClosedHandler(
        tenants=RoutedRepositories(routing, {"adp": ADP_GITHUB_KEY, "beta": BETA_GITHUB_KEY}),
        resolver=_Resolver(),
        signaller=_Signaller(),
    )
    body = _pr_body("beta-web")

    assert handler.verify(body, {"X-Hub-Signature-256": _sign(BETA_GITHUB_KEY, body)})
    assert not handler.verify(body, {"X-Hub-Signature-256": _sign(ADP_GITHUB_KEY, body)})
    unowned = _pr_body("elsewhere")
    assert not handler.verify(unowned, {"X-Hub-Signature-256": _sign(ADP_GITHUB_KEY, unowned)})


async def test_a_beta_pr_delivery_resolves_through_the_beta_tenant(monkeypatch):
    resolver = _Resolver()
    routes = _two_tenant_routes(monkeypatch, resolver, _Starter())
    body = _pr_body("beta-web")

    code, payload = await _post(
        routes["/webhook/pr"],
        body,
        {"X-Hub-Signature-256": _sign(BETA_GITHUB_KEY, body), "X-GitHub-Delivery": "d-1"},
    )

    assert code == 200
    assert payload["workflow_id"] == "wf-beta-T-7"
    assert [event.tenant for event in resolver.events] == ["beta"]


async def test_a_pr_delivery_signed_by_another_tenant_is_refused(monkeypatch):
    resolver = _Resolver()
    routes = _two_tenant_routes(monkeypatch, resolver, _Starter())
    body = _pr_body("beta-web")

    code, _ = await _post(
        routes["/webhook/pr"],
        body,
        {"X-Hub-Signature-256": _sign(ADP_GITHUB_KEY, body), "X-GitHub-Delivery": "d-1"},
    )

    assert code == 401
    assert resolver.events == []


async def test_a_single_tenant_refuses_an_unlisted_repository_as_before(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "one-key")
    settings = hermetic_settings(tenant="adp")
    routing = _routing({"adp": {"adp-web": GITHUB}}, settings=settings)
    route = build_routes_for(settings, routing=routing, signaller=_Signaller(), starter=_Starter())[
        "/webhook/pr"
    ]
    body = _pr_body("elsewhere")

    code, _ = await _post(
        route, body, {"X-Hub-Signature-256": _sign("one-key", body), "X-GitHub-Delivery": "d"}
    )

    assert code == 403


class _Connection:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(first=lambda: None)


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self):
        return self.connection


async def _resolved_sql(event: PrClosedEvent, unattributed: str) -> str:
    engine = _Engine()
    resolver = PostgresWorkflowResolver(cast(AsyncEngine, engine), unattributed_tenant=unattributed)
    await resolver.resolve(event)
    return str(
        engine.connection.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _event(tenant: str | None) -> PrClosedEvent:
    return PrClosedEvent(
        repo="beta-web", number=3, branch="agent/T-7", task_id="T-7", merged=True, tenant=tenant
    )


async def test_the_resolver_matches_the_tenant_and_the_task():
    sql = await _resolved_sql(_event("beta"), "adp")

    assert "executions.task_id = 'T-7'" in sql
    assert "executions.tenant = 'beta'" in sql
    assert "IS NULL" not in sql


async def test_the_home_tenant_also_matches_executions_recorded_without_a_tenant():
    sql = await _resolved_sql(_event("adp"), "adp")

    assert "executions.tenant = 'adp' OR agent_platform.executions.tenant IS NULL" in sql


async def test_the_platform_resolves_by_task_alone():
    sql = await _resolved_sql(_event(""), "")

    assert "executions.tenant" not in sql


async def test_a_jira_delivery_hands_the_issue_key_to_its_tenant(monkeypatch):
    starter = _Starter()
    routes = _two_tenant_routes(monkeypatch, _Resolver(), starter)
    body = _jira_body("BETA-12")

    code, payload = await _post(
        routes["/webhook/jira/beta"], body, {"X-Hub-Signature": _sign(BETA_JIRA_KEY, body)}
    )

    assert code == 200
    assert payload["workflow_id"].startswith("jira-intake-beta-")
    assert [(event.tenant, event.project, event.key) for event in starter.events] == [
        ("beta", "BETA", "BETA-12")
    ]


async def test_a_jira_delivery_signed_by_another_tenant_is_refused(monkeypatch):
    starter = _Starter()
    routes = _two_tenant_routes(monkeypatch, _Resolver(), starter)
    body = _jira_body("BETA-12")

    code, _ = await _post(
        routes["/webhook/jira/beta"], body, {"X-Hub-Signature": _sign(ADP_JIRA_KEY, body)}
    )

    assert code == 401
    assert starter.events == []


async def test_a_project_outside_the_tenant_allowlist_is_refused(monkeypatch):
    starter = _Starter()
    routes = _two_tenant_routes(monkeypatch, _Resolver(), starter)
    body = _jira_body("ADP-12")

    code, _ = await _post(
        routes["/webhook/jira/beta"], body, {"X-Hub-Signature": _sign(BETA_JIRA_KEY, body)}
    )

    assert code == 403
    assert starter.events == []


def test_only_served_tenants_have_a_jira_route(monkeypatch):
    routes = _two_tenant_routes(monkeypatch, _Resolver(), _Starter())

    assert set(routes) == {"/webhook/pr", "/webhook/jira/adp", "/webhook/jira/beta"}


def test_a_single_tenant_keeps_the_shared_jira_route(monkeypatch):
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "one-key")
    settings = hermetic_settings(tenant="adp")
    routing = _routing({"adp": {}}, {"adp": _jira_yaml("ADP")}, settings=settings)

    routes = build_routes_for(settings, routing=routing, signaller=_Signaller(), starter=_Starter())

    assert routes["/webhook/jira"] is routes["/webhook/jira/adp"]
    assert routes["/webhook/jira"].handler._secret == "one-key"  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]


def test_a_jira_allowlist_that_fails_to_parse_keeps_the_last_valid_one():
    sources = {"beta": _Source({"jira": _jira_yaml("BETA")})}
    now = [0.0]
    routing = TenantRouting(
        hermetic_settings(tenants="beta", tenant_config_refresh_seconds=1),
        registries=cast(Any, _Registries({"beta": _Registry({})})),
        source_factory=lambda tenant: cast(Any, sources[tenant]),
        clock=lambda: now[0],
    )
    assert routing.jira_projects("beta") == frozenset({"BETA"})

    sources["beta"].files["jira"] = "allowlist: [unclosed"
    now[0] = 5.0

    assert routing.jira_projects("beta") == frozenset({"BETA"})


def test_the_delivery_id_and_workflow_id_are_per_tenant_and_deterministic():
    starter = _Starter()
    payload = json.loads(_jira_body("BETA-12"))
    beta = TenantJiraIntakeHandler(tenant="beta", secret="k", starter=starter)
    adp = TenantJiraIntakeHandler(tenant="adp", secret="k", starter=starter)
    first, again = beta.parse(payload), beta.parse(payload)

    assert first is not None and again is not None
    assert issue_intake_workflow_id(first) == issue_intake_workflow_id(again)
    assert beta.delivery_id({}, payload) != adp.delivery_id({}, payload)


def test_a_malformed_issue_key_is_ignored():
    handler = TenantJiraIntakeHandler(tenant="beta", secret="k", starter=_Starter())

    assert handler.parse(json.loads(_jira_body('BETA-1" OR project = "ADP'))) is None


@dataclass
class _Client:
    starts: list[dict[str, Any]] = field(default_factory=list)
    started: set[str] = field(default_factory=set)

    async def start_workflow(self, run, params, **options):
        from temporalio.exceptions import WorkflowAlreadyStartedError

        if options["id"] in self.started:
            raise WorkflowAlreadyStartedError(options["id"], "JiraIssueIntakeWorkflow")
        self.started.add(options["id"])
        self.starts.append({"params": params, **options})


async def test_the_intake_workflow_starts_once_on_the_tenant_task_queue():
    client = _Client()
    intake = TemporalIssueIntake(client, hermetic_settings(temporal_task_queue="q"))
    event = TenantIssueEvent(tenant="beta", project="BETA", key="BETA-12", delivery_id="beta:1")

    first = await intake.start(event)
    second = await intake.start(event)

    assert first["status"] == "started"
    assert second == {"status": "duplicate", "workflow_id": first["workflow_id"]}
    assert client.starts[0]["task_queue"] == "q-beta"
    assert client.starts[0]["params"].issue_key == "BETA-12"


class _Modes:
    def webhook_enabled(self, name: str) -> bool:
        return True


def test_several_tenants_without_any_secret_refuse_to_start(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.webhook.server.load_integrations_config", lambda s: _Modes()
    )
    with pytest.raises(SystemExit) as excinfo:
        require_webhook_secrets(hermetic_settings(tenants="adp,beta"), environ={})

    message = str(excinfo.value)
    assert "GITHUB_WEBHOOK_SECRET_BETA" in message
    assert "JIRA_WEBHOOK_SECRET_ADP" in message


def test_one_tenant_with_a_secret_lets_the_webhook_start(monkeypatch):
    monkeypatch.setattr(
        "services.orchestration.webhook.server.load_integrations_config", lambda s: _Modes()
    )
    require_webhook_secrets(
        hermetic_settings(tenants="adp,beta"),
        environ={"GITHUB_WEBHOOK_SECRET_ADP": "a", "JIRA_WEBHOOK_SECRET_BETA": "b"},
    )
