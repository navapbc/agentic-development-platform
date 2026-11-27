from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from scripts import deployment_validate
from services.integration.credentials import ConfigError
from services.integration.tenant_config import Bundle, BundleIdentity
from services.operator_remedy import DeploymentPosture, remedy
from services.validation import dependencies as validation_dependencies
from services.validation import platform as validation_platform
from services.validation import tenant as validation_tenant
from services.validation.checks import Check
from services.validation.report import (
    MODEL_PATH,
    CheckResult,
    Scope,
    Status,
    TenantReport,
    failed,
    passed,
    render_platform_report,
    render_tenant_report,
)
from services.validation.tenant import TenantContext, TenantValidationRequest
from tests.support import hermetic_settings, seed_rebar_store

HOST = DeploymentPosture.HOST
DEPLOYED = DeploymentPosture.DEPLOYED
NOW = datetime(2026, 11, 24, tzinfo=UTC)


def _settings(**overrides: Any):
    values: dict[str, Any] = {"tenant": "adp", "tenants": "adp,beta"}
    values.update(overrides)
    return hermetic_settings(**values)


def _context(**overrides: Any) -> TenantContext:
    transport = overrides.pop("transport", None)
    client = overrides.pop("client", None)
    return TenantContext(_settings(**overrides), client=client, transport=transport, now=NOW)


def _only(results: list[CheckResult]) -> CheckResult:
    assert len(results) == 1
    return results[0]


def _fixes(result: CheckResult) -> tuple[str, str]:
    return result.fix_for(HOST), result.fix_for(DEPLOYED)


class _Source:
    def __init__(self, latest: BundleIdentity, files: dict[str, str]) -> None:
        self._latest, self._files = latest, files
        self.location = "test"

    def latest(self) -> BundleIdentity:
        return self._latest

    def fetch(self, identity: BundleIdentity) -> Bundle:
        return Bundle(identity, self._files)


def _bundle_config(monkeypatch, tmp_path: Path, *, latest_files: dict[str, str] | None = None):
    active = BundleIdentity("adp", "test", {"gates.yaml": "a"})
    latest = BundleIdentity("adp", "test", {"gates.yaml": "b"}) if latest_files else active
    config = SimpleNamespace(
        active=active, active_path=tmp_path, source=_Source(latest, latest_files or {})
    )
    monkeypatch.setattr("services.integration.tenant_config.active_tenant_config", lambda: config)
    return active, latest


async def test_the_bundle_check_passes_on_a_valid_active_bundle(monkeypatch, tmp_path) -> None:
    active, _ = _bundle_config(monkeypatch, tmp_path)
    monkeypatch.setattr("services.integration.tenant_config.validate_bundle", lambda *_: None)

    result = _only(await validation_tenant.check_bundle(_context()))

    assert result.status == Status.PASSED
    assert active.id in result.cause


async def test_a_refused_latest_bundle_fails_with_the_fix_for_each_posture(
    monkeypatch, tmp_path
) -> None:
    _, latest = _bundle_config(monkeypatch, tmp_path, latest_files={"gates.yaml": "broken"})

    def validate(path: Path, _settings) -> None:
        if path != tmp_path:
            raise ConfigError("gates.yaml: unknown gate")

    monkeypatch.setattr("services.integration.tenant_config.validate_bundle", validate)

    result = _only(await validation_tenant.check_bundle(_context()))
    host, deployed = _fixes(result)

    assert result.failed
    assert latest.id in result.cause and "unknown gate" in result.cause
    assert "`inv tenant-config-validate --tenant adp`" in host
    assert "config repository" in deployed and "inv" not in deployed


def _escalation(monkeypatch, *, app: str = "own") -> None:
    slack = SimpleNamespace(app=app, api_base_url="https://slack.com/api/", channel="C1")
    config = SimpleNamespace(channels=("slack",), slack=slack)
    monkeypatch.setattr(validation_tenant, "_escalation", lambda settings: config)


async def test_slack_auth_passes_when_auth_test_answers(monkeypatch) -> None:
    _escalation(monkeypatch)
    monkeypatch.setattr(
        validation_tenant, "_auth_test", lambda token, url: {"user": "adp-bot", "team": "Acme"}
    )

    result = _only(await validation_tenant.check_slack(_context(slack_bot_token="xoxb-1")))

    assert result.status == Status.PASSED
    assert "adp-bot" in result.cause


async def test_a_refused_bot_token_fails_with_the_setting_for_each_posture(monkeypatch) -> None:
    _escalation(monkeypatch)

    class _Refused(Exception):
        response = {"error": "invalid_auth"}

    def refuse(token: str, url: str) -> dict:
        raise _Refused

    monkeypatch.setattr(validation_tenant, "_auth_test", refuse)

    result = _only(await validation_tenant.check_slack(_context(slack_bot_token="xoxb-1")))
    host, deployed = _fixes(result)

    assert result.failed and "invalid_auth" in result.cause
    assert "`SLACK_BOT_TOKEN` in `config/local/tenants/adp.env`" in host
    assert "`slack_bot_token` secret" in deployed
    assert "kubectl -n tenant-adp rollout restart deploy/activity-worker" in deployed


async def test_a_shared_app_tenant_skips_auth_test(monkeypatch) -> None:
    _escalation(monkeypatch, app="shared")

    result = _only(await validation_tenant.check_slack(_context()))

    assert result.status == Status.SKIP


async def test_a_readable_rebar_store_passes(monkeypatch, tmp_path) -> None:
    from services.integration.tickets.rebar_config import PollConfig

    store = tmp_path / "rebar-tickets"
    seed_rebar_store(store)
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_rebar_backend_config",
        lambda: SimpleNamespace(repo_root=str(store)),
    )
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_poll_config",
        lambda: PollConfig(
            reader_roles=("tickets-reader",),
            identity_roles=(("agent-platform@users.noreply.invalid", "tickets-reader"),),
        ),
    )

    result = _only(await validation_tenant.check_ticket_store(_context(ticket_backend="rebar")))

    assert result.status == Status.PASSED
    assert "reads" in result.cause


async def test_a_rebar_store_whose_identity_the_poll_refuses_fails(monkeypatch, tmp_path) -> None:
    store = tmp_path / "rebar-tickets"
    seed_rebar_store(store)
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_rebar_backend_config",
        lambda: SimpleNamespace(repo_root=str(store)),
    )
    monkeypatch.setattr(validation_tenant, "_poll_authorized", lambda root: False)

    result = _only(await validation_tenant.check_ticket_store(_context(ticket_backend="rebar")))

    assert result.status == Status.FAIL
    assert "AUTHORSHIP_IDENTITY_ID" in result.cause


def _synced_store(tmp_path: Path, monkeypatch) -> Path:
    import subprocess

    from services.integration.tickets import store_remote

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    store = tmp_path / "rebar-tickets"
    seed_rebar_store(store)
    subprocess.run(["git", "-C", str(store), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(store), "push", "-q", "origin", "tickets"], check=True)
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_rebar_backend_config",
        lambda: SimpleNamespace(repo_root=str(store)),
    )
    monkeypatch.setattr(
        store_remote,
        "read_store_remote",
        lambda root: store_remote.StoreRemote("origin", "s3://r@b/tickets", "s3://w@b/tickets"),
    )
    return store


def test_a_rebar_store_that_matches_its_bucket_passes_the_sync_check(monkeypatch, tmp_path):
    _synced_store(tmp_path, monkeypatch)

    result = validation_tenant._rebar_sync_result("beta")

    assert result is not None and result.status == Status.PASSED


def test_a_rebar_store_with_unpushed_writes_fails_the_sync_check(monkeypatch, tmp_path):
    import subprocess

    store = _synced_store(tmp_path, monkeypatch)
    worktree = store / ".tickets-tracker"
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "--allow-empty", "-m", "unpushed"],
        check=True,
    )

    result = validation_tenant._rebar_sync_result("beta")

    assert result is not None and result.status == Status.FAIL
    assert "ahead of and 0 commits behind" in result.cause


async def test_a_rebar_store_without_a_repository_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "services.integration.tickets.rebar_config.load_rebar_backend_config",
        lambda: SimpleNamespace(repo_root=str(tmp_path)),
    )

    result = _only(await validation_tenant.check_ticket_store(_context(ticket_backend="rebar")))
    host, deployed = _fixes(result)

    assert result.failed
    assert "`inv k8s-reload`" in host
    assert "kubectl -n tenant-adp rollout restart" in deployed


def _jira(monkeypatch, search) -> None:
    resolver = SimpleNamespace(projects=lambda: {"OPS"})
    monkeypatch.setattr(
        "services.integration.jira.credentials.build_jira_credential_resolver", lambda: resolver
    )
    monkeypatch.setattr(
        "services.integration.jira.adapter.build_jira_adapter",
        lambda project, resolver: SimpleNamespace(client=SimpleNamespace(search_issues=search)),
    )
    monkeypatch.setattr(validation_tenant, "_jira_token_ref", lambda project: "JIRA_API_TOKEN")


async def test_jira_passes_when_the_token_reads_the_project(monkeypatch) -> None:
    _jira(monkeypatch, lambda jql, start_at, max_results: [])

    result = _only(await validation_tenant.check_ticket_store(_context(ticket_backend="jira")))

    assert result.status == Status.PASSED and "OPS" in result.name


async def test_a_refused_jira_search_fails_with_the_token_setting(monkeypatch) -> None:
    def refuse(jql, start_at, max_results):
        raise RuntimeError("Jira Cloud answered 401")

    _jira(monkeypatch, refuse)

    result = _only(await validation_tenant.check_ticket_store(_context(ticket_backend="jira")))
    host, deployed = _fixes(result)

    assert result.failed and "401" in result.cause
    assert "`JIRA_API_TOKEN` in `config/local/tenants/adp.env`" in host
    assert "`jira_api_token` secret" in deployed


def _registry(monkeypatch, tmp_path: Path, backend: str) -> None:
    repository = SimpleNamespace(token_ref="GITHUB_TOKEN")
    registry = SimpleNamespace(
        names=lambda kind=None: {"svc"} if kind in (None, backend) else set(),
        get=lambda name: repository,
        location=lambda name: str(tmp_path / name),
    )
    monkeypatch.setattr(
        "services.integration.repositories.load_repository_registry", lambda settings: registry
    )
    credential = SimpleNamespace(api_url="https://api.github.test", owner="acme", token="t")
    resolver = SimpleNamespace(resolve=lambda name: credential)
    monkeypatch.setattr(
        "services.integration.github.credentials.build_github_credential_resolver",
        lambda settings: resolver,
    )


def _github(status: int, expiration: str = "") -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/svc"
        assert request.headers["Authorization"] == "Bearer t"
        headers = {validation_tenant.GITHUB_EXPIRY_HEADER: expiration} if expiration else {}
        return httpx.Response(status, headers=headers, json={})

    return httpx.MockTransport(answer)


async def test_a_github_token_that_reads_the_repository_passes(monkeypatch, tmp_path) -> None:
    _registry(monkeypatch, tmp_path, "github")
    context = _context(transport=_github(200, "2027-06-01 00:00:00 UTC"))

    result = _only(await validation_tenant.check_source_control(context))

    assert result.status == Status.PASSED
    assert "2027-06-01" in result.cause


@pytest.mark.parametrize(
    ("status", "expiration", "named"),
    [(401, "", "401"), (200, "2026-11-27 00:00:00 UTC", "expires at 2026-11-27")],
)
async def test_a_refused_or_expiring_github_token_fails_for_each_posture(
    monkeypatch, tmp_path, status, expiration, named
) -> None:
    _registry(monkeypatch, tmp_path, "github")
    context = _context(transport=_github(status, expiration))

    result = _only(await validation_tenant.check_source_control(context))
    host, deployed = _fixes(result)

    assert result.failed and named in result.cause
    assert "`GITHUB_TOKEN` in `config/local/tenants/adp.env`" in host
    assert "`github_token` secret" in deployed


async def test_a_local_repository_passes_only_with_its_checkout(monkeypatch, tmp_path) -> None:
    _registry(monkeypatch, tmp_path, "local")

    missing = _only(await validation_tenant.check_source_control(_context()))
    (tmp_path / "svc" / ".git").mkdir(parents=True)
    present = _only(await validation_tenant.check_source_control(_context()))

    assert missing.failed and "`inv repos-sync`" in missing.fix_for(HOST)
    assert present.status == Status.PASSED


def _gateway(body: str, status: int = 500) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=body))


async def test_the_model_path_fails_on_a_named_hop_with_the_fix_for_each_posture() -> None:
    expired = "botocore ExpiredToken: The security token included in the request is expired"
    context = _context(transport=_gateway(expired))

    result = _only(await validation_tenant.check_model_path(context))
    host, deployed = _fixes(result)

    assert result.failed
    assert host == "Run `inv aws-login`."
    assert "terraform apply" in deployed and "Pod Identity" in deployed


async def test_the_model_path_passes_when_the_call_answers() -> None:
    context = _context(transport=_gateway("{}", 200))

    assert _only(await validation_tenant.check_model_path(context)).status == Status.PASSED


class _Temporal:
    namespace = "default"

    def __init__(self, pollers: dict[tuple[str, int], int]) -> None:
        self._pollers = pollers
        self.workflow_service = self

    async def describe_task_queue(self, request) -> SimpleNamespace:
        count = self._pollers.get((request.task_queue.name, request.task_queue_type), 0)
        return SimpleNamespace(pollers=[object()] * count)


async def test_temporal_passes_only_while_a_workflow_worker_polls_the_tenant_queue() -> None:
    polled = _Temporal({("agent-platform-adp", 1): 1})

    ok = _only(await validation_tenant.check_temporal(_context(client=polled)))
    idle = _only(await validation_tenant.check_temporal(_context(client=_Temporal({}))))

    assert ok.status == Status.PASSED
    assert idle.failed and "`inv k8s-reload`" in idle.fix_for(HOST)
    assert "`inv platform-deploy`" in idle.fix_for(DEPLOYED)


async def test_the_schema_check_names_the_migration_for_each_posture(monkeypatch) -> None:
    from database.schema_guard import migration_script

    heads = set(migration_script().get_heads())

    async def behind(url: str) -> set[str]:
        return set()

    async def current(url: str) -> set[str]:
        return heads

    monkeypatch.setattr("database.schema_guard.applied_revisions", behind)
    stale = await validation_dependencies.schema_result(_settings())
    monkeypatch.setattr("database.schema_guard.applied_revisions", current)
    fresh = await validation_dependencies.schema_result(_settings())

    assert stale.failed
    assert stale.fix_for(HOST) == "Run `inv db-migrate`."
    assert "`inv platform-deploy`" in stale.fix_for(DEPLOYED)
    assert fresh.status == Status.PASSED


def _litellm(answers: dict[str, httpx.Response]) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": alias} for alias in answers]})
        return answers[json.loads(request.content)["model"]]

    return httpx.MockTransport(answer)


async def test_each_model_alias_is_called_and_a_failing_hop_names_its_fix() -> None:
    transport = _litellm(
        {
            "planning-model": httpx.Response(200, json={}),
            "review-model": httpx.Response(
                500, text="litellm.APIConnectionError: bedrock Cannot connect to host"
            ),
        }
    )
    context = validation_platform.PlatformContext(_settings(), [], transport=transport)

    results = {r.name: r for r in await validation_platform.check_model_aliases(context)}

    assert results["Model alias planning-model"].status == Status.PASSED
    failing = results["Model alias review-model"]
    assert failing.failed and "bedrock_path" in failing.cause
    assert failing.fix_for(HOST) == "Run `inv tunnel-start --detach`."
    assert "Bedrock runtime interface endpoint" in failing.fix_for(DEPLOYED)


def _session(tmp_path: Path, expires: datetime) -> Path:
    path = tmp_path / "credentials.json"
    record = {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}
    record["Expiration"] = expires.isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_the_local_session_passes_with_the_identity_sts_names(tmp_path) -> None:
    path = _session(tmp_path, NOW + timedelta(hours=6))
    context = validation_platform.PlatformContext(
        _settings(aws_credentials_file=str(path)),
        [],
        sts=lambda record: {"Arn": "arn:aws:sts::9:assumed-role/dev/me"},
        now=NOW,
    )

    result = validation_platform._session_result(context)

    assert result.status == Status.PASSED
    assert "arn:aws:sts::9:assumed-role/dev/me" in result.cause


def test_an_expired_local_session_fails_with_the_fix_for_each_posture(tmp_path) -> None:
    path = _session(tmp_path, NOW - timedelta(minutes=1))
    context = validation_platform.PlatformContext(
        _settings(aws_credentials_file=str(path)), [], now=NOW
    )

    result = validation_platform._session_result(context)

    assert result.failed and "expired" in result.cause
    assert result.fix_for(HOST) == "Run `inv aws-login`."
    assert "Pod Identity" in result.fix_for(DEPLOYED)


async def test_in_eks_a_refused_signature_fails_the_identity_check() -> None:
    expired = "UnrecognizedClientException: The security token included in the request is invalid"
    context = validation_platform.PlatformContext(
        _settings(deployment_posture="deployed"), [], transport=_gateway(expired, 400)
    )

    result = _only(await validation_platform.check_aws_identity(context))

    assert result.failed
    assert "terraform apply" in result.fix_for(DEPLOYED)


async def test_each_tenant_queue_needs_a_workflow_and_an_activity_poller() -> None:
    client = _Temporal({("agent-platform-adp", 1): 1, ("agent-platform-adp", 2): 1})
    context = validation_platform.PlatformContext(_settings(), ["adp", "beta"], client=client)

    results = {r.name: r for r in await validation_platform.check_temporal(context)}

    assert results["Task queue agent-platform-adp"].status == Status.PASSED
    assert results["Task queue agent-platform-beta"].failed


def _check(name: str, scope: str, result: CheckResult) -> Check:
    async def run(context) -> list[CheckResult]:
        return [result]

    return Check(name, scope, run, dependency=MODEL_PATH if scope == Scope.PLATFORM else "")


LEAKY = failed(
    MODEL_PATH,
    "http://litellm:4000 answered for arn:aws:iam::9:role/beta-worker of tenant beta",
    remedy("Run `inv aws-login`."),
    confirm="Validate again.",
)
OWN_FAILURE = failed(
    "Slack auth",
    "Slack refused auth.test with invalid_auth",
    remedy("Set the bot token."),
    confirm="Validate again.",
)


async def _tenant_report(request: TenantValidationRequest, own: CheckResult) -> TenantReport:
    checks = (
        _check("Slack auth", Scope.TENANT, own),
        _check(MODEL_PATH, Scope.PLATFORM, LEAKY),
    )
    return await validation_tenant.validate_tenant(_settings(), request, checks=checks)


async def test_a_tenant_report_names_its_dependencies_without_platform_detail() -> None:
    report = await _tenant_report(
        TenantValidationRequest(platform_failures=["Temporal"]), OWN_FAILURE
    )

    for posture in (HOST, DEPLOYED):
        text = render_tenant_report(report, posture)
        assert "FAIL Model path" in text and "FAIL Temporal" in text
        assert "invalid_auth" in text
        for internal in ("litellm:4000", "arn:aws", "beta"):
            assert internal not in text


async def test_the_scheduled_validation_alerts_only_failures_to_the_owning_tenant(
    monkeypatch,
) -> None:
    sent: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        "services.orchestration.notifications.deliver_alert",
        lambda message, settings: sent.append((message, settings)) or ["slack"],
    )
    request = TenantValidationRequest(alert_failures=True)

    await _tenant_report(request, passed("Slack auth"))
    alerted = len(sent)
    report = await _tenant_report(request, OWN_FAILURE)

    assert alerted == 1
    assert report.failed and len(sent) == 2
    message, settings = sent[1]
    assert settings.tenant == "adp"
    assert "PASS" not in message.body
    assert "FAIL Slack auth" in message.body and "FAIL Model path" in message.body
    assert "beta" not in message.body and "litellm:4000" not in message.body


async def test_a_passing_scheduled_validation_sends_no_alert(monkeypatch) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(
        "services.orchestration.notifications.deliver_alert",
        lambda message, settings: sent.append(message) or [],
    )
    checks = (_check("Slack auth", Scope.TENANT, passed("Slack auth")),)

    await validation_tenant.validate_tenant(
        _settings(), TenantValidationRequest(alert_failures=True), checks=checks
    )

    assert sent == []


async def _aggregate(tenants: list[str], platform: CheckResult) -> tuple[Any, list]:
    calls: list[tuple[str, TenantValidationRequest]] = []

    async def run_tenant(client, settings, tenant, request) -> TenantReport:
        calls.append((tenant, request))
        return TenantReport(tenant, [passed("Slack auth")])

    report = await validation_platform.validate_deployment(
        _settings(),
        tenants=tenants,
        request=TenantValidationRequest(alert_failures=True),
        checks=(_check(MODEL_PATH, Scope.PLATFORM, platform),),
        run_tenant=run_tenant,
    )
    return report, calls


async def test_all_tenants_runs_every_tenant_with_the_platform_failures_by_name() -> None:
    known = ["adp", "beta"]
    tenants = deployment_validate.scope_tenants(
        deployment_validate.parse_args(["--all-tenants"]), known
    )

    report, calls = await _aggregate(tenants, LEAKY)

    assert [tenant for tenant, _ in calls] == known
    assert all(request.platform_failures == [MODEL_PATH] for _, request in calls)
    assert all(request.alert_failures for _, request in calls)
    text = render_platform_report(report, HOST)
    assert "Deployment validation of the platform" in text
    assert "Deployment validation of tenant adp" in text
    assert "Deployment validation of tenant beta" in text
    assert report.failed


async def test_one_tenant_runs_only_that_tenant_and_an_unknown_tenant_is_refused() -> None:
    args = deployment_validate.parse_args(["--tenant", "beta"])

    _, calls = await _aggregate(deployment_validate.scope_tenants(args, ["adp", "beta"]), LEAKY)

    assert [tenant for tenant, _ in calls] == ["beta"]
    with pytest.raises(SystemExit, match="not gamma"):
        deployment_validate.scope_tenants(
            deployment_validate.parse_args(["--tenant", "gamma"]), ["adp", "beta"]
        )


async def test_a_tenant_whose_validation_cannot_start_is_reported_as_failed() -> None:
    report = await validation_platform.tenant_report(
        None, _settings(), "beta", TenantValidationRequest()
    )

    assert report.tenant == "beta" and report.failed
    assert "deploy/activity-worker" in report.checks[0].fix_for(HOST)


async def test_a_posted_report_is_the_slack_post_check(monkeypatch) -> None:
    posted: list[str] = []
    monkeypatch.setattr(validation_tenant, "_escalation", lambda settings: SimpleNamespace())

    def deliver(self, message, settings, config) -> bool:
        posted.append(message.body)
        return True

    monkeypatch.setattr("services.orchestration.notifications.SlackChannel.deliver", deliver)

    report = await _tenant_report(TenantValidationRequest(post=True), passed("Slack auth"))

    assert "PASS Slack post" in posted[0]
    assert [c.name for c in report.checks if c.status == Status.PASSED][-1] == "Slack post"


async def test_a_failed_post_fails_the_slack_post_check(monkeypatch) -> None:
    monkeypatch.setattr(validation_tenant, "_escalation", lambda settings: SimpleNamespace())
    monkeypatch.setattr(
        "services.orchestration.notifications.SlackChannel.deliver",
        lambda self, message, settings, config: False,
    )

    report = await _tenant_report(TenantValidationRequest(post=True), passed("Slack auth"))
    post = next(c for c in report.checks if c.name == "Slack post")

    assert post.failed
    assert "`slack.channel` in `config/local/tenants/adp/escalation.yaml`" in post.fix_for(HOST)
    assert "tenant config bundle" in post.fix_for(DEPLOYED)


async def _through_temporal() -> tuple[TenantReport, list]:
    from services.orchestration.temporal_workflows.validation import TenantValidationWorkflow
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    received: list[TenantValidationRequest] = []

    @activity.defn(name="validate_tenant")
    async def fake_validate(request: TenantValidationRequest) -> TenantReport:
        received.append(request)
        return TenantReport("adp", [OWN_FAILURE])

    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue="agent-platform-adp",
            workflows=[TenantValidationWorkflow],
            activities=[fake_validate],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
        ),
    ):
        report = await validation_platform.tenant_report(
            env.client,
            _settings(),
            "adp",
            TenantValidationRequest(platform_failures=[MODEL_PATH]),
        )
    return report, received


async def test_a_tenant_validation_runs_on_the_tenants_queue_and_returns_its_report() -> None:
    report, received = await _through_temporal()

    assert received[0].platform_failures == [MODEL_PATH]
    assert report.tenant == "adp"
    assert report.checks[0].fix_for(HOST) == "Set the bot token."


@pytest.mark.parametrize("flag", ["--alert-failures", "--alert-platform"])
def test_platform_failures_reach_the_administrators_and_not_the_tenants(monkeypatch, flag):
    from services.orchestration import notifications
    from services.validation.report import (
        MODEL_PATH,
        PlatformReport,
        Scope,
        TenantReport,
        dependencies,
        failure_alert,
    )
    from services.validation.report import failed as failed_check

    cause = "the alias adp-claude is missing from https://litellm.adp-platform.svc:4000"
    fix = "Run kubectl -n adp-platform rollout restart deployment/litellm"
    platform_check = failed_check(
        validation_platform.MODEL_ALIASES,
        cause,
        remedy(fix),
        confirm="Run inv deployment-validate --platform.",
        scope=Scope.PLATFORM,
        dependency=MODEL_PATH,
    )
    tenant = TenantReport(
        tenant="acme",
        dependencies=dependencies([platform_check], [MODEL_PATH]),
    )
    report = PlatformReport(checks=[platform_check], tenants=[tenant])
    admin, tenant_alerts = [], []

    async def _validate(*args, **kwargs):
        return report

    async def _unreachable(*args, **kwargs):
        raise ConnectionError("no Temporal")

    monkeypatch.setattr(validation_platform, "validate_deployment", _validate)
    monkeypatch.setattr("services.temporal_client.connect", _unreachable)
    monkeypatch.setattr(
        notifications, "deliver_admin_alert", lambda message, settings: admin.append(message)
    )
    monkeypatch.setattr(
        notifications, "deliver_alert", lambda message, settings: tenant_alerts.append(message)
    )
    monkeypatch.setattr("scripts.config.get_settings", lambda: hermetic_settings(tenants="acme"))

    import asyncio

    asyncio.run(deployment_validate._run(deployment_validate.parse_args(["--all-tenants", flag])))

    [message] = admin
    assert cause in message.body and fix in message.body
    assert tenant_alerts == []
    _, tenant_body = failure_alert(tenant, DeploymentPosture.HOST)
    assert cause not in tenant_body and fix not in tenant_body
    assert MODEL_PATH in tenant_body


def test_a_rebar_store_with_a_history_of_its_own_names_the_reseed(monkeypatch, tmp_path):
    import subprocess

    store = _synced_store(tmp_path, monkeypatch)
    other = tmp_path / "other-store"
    seed_rebar_store(other)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "-C", str(other), "push", "-q", "-f", str(remote), "tickets"], check=True
    )

    result = validation_tenant._rebar_sync_result("beta")

    assert result is not None and result.status == Status.FAIL
    assert "root commit" in result.cause
    assert f"mv {store} {store}.diverged" in str(result.fix)
