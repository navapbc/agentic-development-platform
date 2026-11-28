from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlencode

import pytest
from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.repositories import GITHUB, LOCAL
from services.integration.slack.tenant_settings import TenantSlackSettings
from services.integration.tenant_config import Bundle, BundleIdentity
from services.integration.tenant_secrets import tenant_secrets
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
    Route,
    build_routes_for,
    require_webhook_secrets,
    route_inbound,
    shared_slack_routes,
    slack_routes,
    webhook_tenants,
)
from services.orchestration.webhook.tenants import (
    RoutedRepositories,
    TenantRouting,
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

    def fetch_files(self, identity: BundleIdentity, names: Iterable[str]) -> dict[str, str]:
        return {name: self.files[name] for name in names if name in self.files}


def _jira_yaml(*projects: str) -> str:
    return json.dumps({"config_version": 99, "allowlist": list(projects)})


def _modes(**modes: str) -> str:
    return json.dumps({"config_version": 1, "modes": modes})


WEBHOOK_MODES = _modes(pr_closed=WEBHOOK, jira_intake=WEBHOOK)


def _routing(
    by_tenant: dict[str, dict[str, str]],
    jira: dict[str, str] | None = None,
    *,
    settings: Any = None,
    files: dict[str, dict[str, str]] | None = None,
) -> TenantRouting:
    bundles = {tenant: {"integrations": WEBHOOK_MODES} for tenant in by_tenant}
    for tenant, named in (files or {}).items():
        bundles.setdefault(tenant, {}).update(named)
    for tenant, text in (jira or {}).items():
        bundles.setdefault(tenant, {})["jira"] = text
    sources = {tenant: _Source(named) for tenant, named in bundles.items()}
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
    return await route_inbound(route, body, headers, ledger=InMemorySideEffectLedger())


def test_each_github_repository_routes_to_the_tenant_that_lists_it():
    routing = _routing(
        {"adp": {"adp-web": GITHUB, "adp-local": LOCAL}, "beta": {"beta-web": GITHUB}}
    )

    assert routing.repository_owners() == {"adp-web": "adp", "beta-web": "beta"}
    assert routing.github_repositories() == frozenset({"adp-web", "beta-web"})


def test_a_repository_two_tenants_list_is_kept_by_both():
    routing = _routing({"adp": {"shared": GITHUB}, "beta": {"shared": GITHUB}})

    assert routing.claimants("shared") == ("adp", "beta")
    assert routing.owner("shared") is None
    assert "shared" in routing.github_repositories()
    assert "shared" not in routing.repository_owners()


def test_a_shared_repository_routes_each_delivery_by_its_signature():
    routing = _routing({"adp": {"shared": GITHUB}, "beta": {"shared": GITHUB}})
    tenants = RoutedRepositories(routing, {"adp": ADP_GITHUB_KEY, "beta": BETA_GITHUB_KEY})
    body = _pr_body("shared")

    assert tenants.select_tenant("shared", body, _sign(ADP_GITHUB_KEY, body)) == "adp"
    assert tenants.select_tenant("shared", body, _sign(BETA_GITHUB_KEY, body)) == "beta"
    assert tenants.select_tenant("shared", body, _sign("neither-key", body)) is None


def test_two_tenants_that_share_a_secret_are_refused(caplog: pytest.LogCaptureFixture):
    routing = _routing({"adp": {"shared": GITHUB}, "beta": {"shared": GITHUB}})
    tenants = RoutedRepositories(routing, {"adp": "same-key", "beta": "same-key"})
    body = _pr_body("shared")

    assert tenants.select_tenant("shared", body, _sign("same-key", body)) is None
    assert "tenants adp, beta share a signing key for repository shared" in caplog.text


def test_a_tenant_secret_is_its_own_and_one_tenant_takes_no_unsuffixed_secret():
    unsuffixed = {"GITHUB_WEBHOOK_SECRET": "platform-key"}

    assert tenant_secrets(["adp", "beta"], "GITHUB_WEBHOOK_SECRET_", ENVIRON) == {
        "adp": ADP_GITHUB_KEY,
        "beta": BETA_GITHUB_KEY,
    }
    assert tenant_secrets(["adp"], "GITHUB_WEBHOOK_SECRET_", unsuffixed) == {}
    assert tenant_secrets([""], "GITHUB_WEBHOOK_SECRET_", ENVIRON) == {}


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
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_ADP", "one-key")
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


async def _resolved_sql(event: PrClosedEvent) -> str:
    engine = _Engine()
    resolver = PostgresWorkflowResolver(cast(AsyncEngine, engine))
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
    sql = await _resolved_sql(_event("beta"))

    assert "executions.task_id = 'T-7'" in sql
    assert "executions.tenant = 'beta'" in sql
    assert "IS NULL" not in sql


async def test_a_tenant_event_does_not_match_executions_recorded_without_a_tenant():
    sql = await _resolved_sql(_event("adp"))

    assert "executions.tenant = 'adp'" in sql
    assert "IS NULL" not in sql


async def test_the_platform_resolves_by_task_alone():
    sql = await _resolved_sql(_event(""))

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
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET_ADP", "one-key")
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


def test_several_tenants_without_any_secret_refuse_to_start():
    with pytest.raises(SystemExit) as excinfo:
        require_webhook_secrets(
            hermetic_settings(tenants="adp,beta"), environ={}, modes=lambda tenant: _Modes()
        )

    message = str(excinfo.value)
    assert "GITHUB_WEBHOOK_SECRET_BETA" in message
    assert "JIRA_WEBHOOK_SECRET_ADP" in message


def test_one_tenant_with_a_secret_lets_the_webhook_start():
    require_webhook_secrets(
        hermetic_settings(tenants="adp,beta"),
        environ={"GITHUB_WEBHOOK_SECRET_ADP": "a", "JIRA_WEBHOOK_SECRET_BETA": "b"},
        modes=lambda tenant: _Modes(),
    )


class _TenantModes:
    def __init__(self, tenant: str) -> None:
        self._tenant = tenant

    def webhook_enabled(self, name: str) -> bool:
        return self._tenant == "beta"


def test_only_the_tenants_that_take_webhook_deliveries_need_a_secret():
    require_webhook_secrets(
        hermetic_settings(tenants="adp,beta"),
        environ={"GITHUB_WEBHOOK_SECRET_BETA": "b", "JIRA_WEBHOOK_SECRET_BETA": "b"},
        modes=_TenantModes,
    )


async def test_a_tenant_in_polling_mode_does_not_take_a_signed_delivery(monkeypatch):
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    settings = hermetic_settings(tenants="adp,beta", tenant="adp")
    routing = _routing(
        {"adp": {}, "beta": {}},
        {"adp": _jira_yaml("ADP"), "beta": _jira_yaml("BETA")},
        settings=settings,
        files={"beta": {"integrations": _modes(jira_intake="polling")}},
    )
    starter = _Starter()
    routes = build_routes_for(settings, routing=routing, signaller=_Signaller(), starter=starter)
    adp_body, beta_body = _jira_body("ADP-1"), _jira_body("BETA-1")

    adp = await _post(
        routes["/webhook/jira/adp"], adp_body, {"X-Hub-Signature": _sign(ADP_JIRA_KEY, adp_body)}
    )
    beta = await _post(
        routes["/webhook/jira/beta"],
        beta_body,
        {"X-Hub-Signature": _sign(BETA_JIRA_KEY, beta_body)},
    )

    assert adp[0] == 200
    assert beta == (202, {"status": "webhook disabled", "integration": "jira_intake"})
    assert [event.tenant for event in starter.events] == ["adp"]


async def test_a_single_tenant_webhook_starts_a_bare_route_delivery_on_the_tenant_queue(
    monkeypatch,
):
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET_ADP", "one-key")
    client = _Client()
    settings = hermetic_settings(temporal_task_queue="q", tenant="adp")
    routes = build_routes_for(
        settings,
        routing=_routing({"adp": {}}, {"adp": _jira_yaml("ADP")}, settings=settings),
        signaller=_Signaller(),
        starter=TemporalIssueIntake(client, settings),
    )
    body = _jira_body("ADP-12")

    status, _ = await route_inbound(
        routes["/webhook/jira"],
        body,
        {"X-Hub-Signature": _sign("one-key", body)},
        ledger=InMemorySideEffectLedger(),
    )

    assert status == 200
    assert [start["task_queue"] for start in client.starts] == ["q-adp"]


async def test_a_delivery_no_tenant_owns_starts_nothing_and_is_not_recorded():
    client = _Client()
    starter = TemporalIssueIntake(client, hermetic_settings(temporal_task_queue="q"))
    route = Route(
        handler=TenantJiraIntakeHandler(tenant="", secret="one-key", starter=starter),
        allowed=lambda: frozenset({"ADP"}),
        mode=lambda tenant: WEBHOOK,
    )
    ledger = InMemorySideEffectLedger()
    body = _jira_body("ADP-12")
    headers = {"X-Hub-Signature": _sign("one-key", body)}

    first = await route_inbound(route, body, headers, ledger=ledger)
    again = await route_inbound(route, body, headers, ledger=ledger)

    assert first[0] == again[0] == 404
    assert "no activity worker" in first[1]["error"]
    assert client.starts == []


def test_a_webhook_without_a_tenant_refuses_to_start():
    with pytest.raises(SystemExit, match="serves no tenant"):
        webhook_tenants(hermetic_settings(tenants="", tenant=""))


async def test_the_webhook_resolves_no_workflow_for_an_execution_recorded_without_a_tenant(
    monkeypatch,
):
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    settings = hermetic_settings(tenant="adp")
    routing = _routing({"adp": {"adp-web": GITHUB}}, settings=settings)

    routes = build_routes_for(settings, routing=routing, signaller=_Signaller(), starter=_Starter())

    resolver = routes["/webhook/pr"].handler._resolver  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]
    engine = _Engine()
    resolver._engine = engine  # noqa: SLF001

    assert await resolver.resolve(_event("adp")) is None
    sql = str(
        engine.connection.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "executions.tenant = 'adp'" in sql
    assert "IS NULL" not in sql


def _escalation_yaml(app_id: str) -> str:
    return (
        "config_version: 1\n"
        "channels: [jira, slack]\n"
        "slack:\n"
        "  transport: webhook\n"
        "  channel: C0123\n"
        "  organization_id: T0123\n"
        f"  app_id: {app_id}\n"
        "  profile: private\n"
        "  content: full\n"
    )


def _slack_request(secret: str, app_id: str) -> tuple[bytes, dict[str, str]]:
    payload = {"type": "view_submission", "api_app_id": app_id}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{stamp}:".encode() + body, hashlib.sha256)
    return body, {
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": f"v0={digest.hexdigest()}",
    }


@pytest.mark.parametrize(
    ("apps", "secrets"),
    [
        (
            {"adp": "A0SAME", "beta": "A0SAME", "gamma": "A0GAMMA"},
            {"adp": "adp-signing", "beta": "beta-signing", "gamma": "gamma-signing"},
        ),
        (
            {"adp": "A0ADP", "beta": "A0BETA", "gamma": "A0GAMMA"},
            {"adp": "shared-signing", "beta": "shared-signing", "gamma": "gamma-signing"},
        ),
    ],
    ids=["same-app-id", "same-signing-secret"],
)
async def test_two_tenants_that_share_a_slack_app_or_signing_secret_are_refused(
    monkeypatch, caplog: pytest.LogCaptureFixture, apps: dict[str, str], secrets: dict[str, str]
):
    for tenant, secret in secrets.items():
        monkeypatch.setenv(f"SLACK_SIGNING_SECRET_{tenant.upper()}", secret)
        monkeypatch.setenv(f"ESCALATION_ACTION_KEY_{tenant.upper()}", f"{tenant}-action-key")
    settings = hermetic_settings(tenants="adp,beta,gamma")
    routing = _routing(
        {"adp": {}, "beta": {}, "gamma": {}},
        settings=settings,
        files={tenant: {"escalation": _escalation_yaml(app)} for tenant, app in apps.items()},
    )
    slack = TenantSlackSettings(settings, routing)
    slack.refresh()

    routes = slack_routes(
        settings,
        routing,
        slack=slack.slack,
        decisions=cast(Any, None),
        records=cast(Any, None),
    )

    statuses = {}
    for tenant in ("adp", "beta", "gamma"):
        body, headers = _slack_request(secrets[tenant], apps[tenant])
        statuses[tenant] = (await routes[f"/webhook/slack/{tenant}"].handle(body, headers)).status
    assert statuses == {"adp": 404, "beta": 404, "gamma": 200}
    assert any("adp, beta" in record.getMessage() for record in caplog.records)


class _CountingSource(_Source):
    def __init__(self, files: dict[str, str], reads: list[str]) -> None:
        super().__init__(files)
        self.reads = reads

    def latest(self) -> BundleIdentity:
        self.reads.append(threading.current_thread().name)
        return super().latest()


class _CountingRouting:
    def __init__(self, files: dict[str, dict[str, str]]) -> None:
        self.files = files
        self.reads: list[str] = []

    def tenants(self) -> list[str]:
        return list(self.files)

    def source(self, tenant: str) -> _CountingSource:
        return _CountingSource(self.files[tenant], self.reads)


async def test_a_click_reads_the_slack_snapshot_and_the_refresh_reads_bundles_on_a_thread():
    routing = _CountingRouting({"adp": {"escalation": _escalation_yaml("A0ADP")}})
    settings = hermetic_settings(tenants="adp", tenant_config_refresh_seconds=0)
    slack = TenantSlackSettings(settings, cast(Any, routing))
    slack.refresh()
    reads = len(routing.reads)

    for _ in range(3):
        served = slack.slack("adp")
        assert served is not None and served.app_id == "A0ADP"
    assert len(routing.reads) == reads

    slack.start()
    for _ in range(100):
        if len(routing.reads) > reads:
            break
        await asyncio.sleep(0.01)
    cast(Any, slack)._refreshing.cancel()
    assert routing.reads[reads:]
    assert threading.current_thread().name not in routing.reads[reads:]


@pytest.mark.parametrize(
    ("secret", "paths"),
    [("shared-signing", ["/webhook/slack/_shared"]), ("", [])],
    ids=["shared-secret", "no-shared-secret"],
)
def test_the_shared_slack_route_serves_when_the_shared_signing_secret_is_set(
    monkeypatch, secret: str, paths: list[str]
):
    monkeypatch.setenv("ESCALATION_ACTION_KEY_ADP", "adp-action-key")
    settings = hermetic_settings(tenants="adp", slack_shared_signing_secret=secret)
    routing = _routing({"adp": {}}, settings=settings)

    routes = shared_slack_routes(
        settings,
        routing,
        slack=lambda tenant: None,
        channel_tenant=lambda channel: None,
        shared_app=lambda: None,
        decisions=cast(Any, None),
        records=cast(Any, None),
        retained_holder=cast(Any, None),
    )

    assert sorted(routes) == paths


@pytest.mark.parametrize(
    ("secret", "token", "paths", "sockets"),
    [
        ("shared-signing", "", ["/webhook/slack/_shared"], 0),
        ("", "xapp-shared", [], 1),
        ("shared-signing", "xapp-shared", ["/webhook/slack/_shared"], 1),
    ],
    ids=["request-url", "socket-mode", "both"],
)
def test_the_shared_app_takes_clicks_over_each_transport_its_credentials_select(
    monkeypatch, secret: str, token: str, paths: list[str], sockets: int
):
    from services.orchestration.webhook import server as webhook_server

    started: list[str] = []
    monkeypatch.setattr(
        webhook_server.SharedSlackSocket, "start", lambda self: started.append(self._app_token)
    )
    settings = hermetic_settings(
        tenants="adp", slack_shared_signing_secret=secret, slack_shared_app_token=token
    )
    route = cast(Any, object())

    served = webhook_server.shared_slack_delivery(
        settings, route, lambda: None, asyncio.new_event_loop()
    )

    assert sorted(served) == paths
    assert started == [token] * sockets


class _FlakySource(_Source):
    def __init__(self, files: dict[str, str], failures: int) -> None:
        super().__init__(files)
        self.failures = failures

    def fetch_files(self, identity: BundleIdentity, names: Iterable[str]) -> dict[str, str]:
        names = list(names)
        if self.failures and "integrations" in names:
            self.failures -= 1
            raise OSError("the config bucket did not answer")
        return super().fetch_files(identity, names)


def _mode_routing(source: _Source, clock: list[float]) -> TenantRouting:
    return TenantRouting(
        hermetic_settings(tenants="adp", tenant_config_refresh_seconds=60),
        registries=cast(Any, _Registries({"adp": _Registry({})})),
        source_factory=lambda tenant: cast(Any, source),
        clock=lambda: clock[0],
    )


def test_a_tenant_without_integrations_takes_the_platform_modes(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(tmp_path))
    (tmp_path / "integrations.yaml").write_text(_modes(pr_closed=WEBHOOK))

    routing = _mode_routing(_Source({}), [0.0])

    assert routing.mode("adp", "pr_closed") == WEBHOOK
    assert routing.mode("adp", "jira_intake") == "polling"


def test_an_invalid_integrations_publish_keeps_the_last_valid_modes():
    source = _Source({"integrations": _modes(pr_closed=WEBHOOK)})
    clock = [0.0]
    routing = _mode_routing(source, clock)
    assert routing.mode("adp", "pr_closed") == WEBHOOK

    source.files["integrations"] = _modes(pr_closed="sometimes")
    clock[0] = 61.0

    assert routing.mode("adp", "pr_closed") == WEBHOOK


def test_modes_are_unknown_until_a_first_read_succeeds_and_the_read_is_retried_soon():
    from services.orchestration.webhook.tenants import MODE_RETRY_SECONDS

    source = _FlakySource({"integrations": _modes(pr_closed=WEBHOOK)}, failures=1)
    clock = [0.0]
    routing = _mode_routing(source, clock)

    assert routing.mode("adp", "pr_closed") is None
    clock[0] = MODE_RETRY_SECONDS
    assert routing.mode("adp", "pr_closed") == WEBHOOK


async def test_a_signed_delivery_of_a_tenant_whose_modes_are_unknown_is_answered_503(
    monkeypatch,
):
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    settings = hermetic_settings(tenants="adp", tenant="adp")
    source = _FlakySource({"integrations": WEBHOOK_MODES, "jira": _jira_yaml("ADP")}, failures=1)
    routing = TenantRouting(
        settings,
        registries=cast(Any, _Registries({"adp": _Registry({})})),
        source_factory=lambda tenant: cast(Any, source),
        clock=lambda: 0.0,
    )
    routes = build_routes_for(settings, routing=routing, signaller=_Signaller(), starter=_Starter())
    body = _jira_body("ADP-1")

    code, payload = await _post(
        routes["/webhook/jira/adp"], body, {"X-Hub-Signature": _sign(ADP_JIRA_KEY, body)}
    )

    assert code == 503
    assert "redeliver" in payload["error"]


def test_one_tenant_without_its_own_secret_is_not_served_by_the_unsuffixed_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "platform-key")
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET_ADP", raising=False)

    with pytest.raises(SystemExit, match="GITHUB_WEBHOOK_SECRET_ADP"):
        require_webhook_secrets(
            hermetic_settings(tenants="adp", tenant="adp"),
            modes=lambda tenant: cast(Any, SimpleNamespace(webhook_enabled=lambda name: True)),
        )
