"""Tenant validation, which runs in the tenant's activity worker with the tenant's own settings.

The worker holds only its tenant's credentials and config bundle, so nothing a check reads can
name another tenant. The platform dependencies the tenant relies on, the model path, Temporal,
and the database schema, are checked here too and reported as one pass or fail line each. Their
detail goes to the worker log, which the platform administrator reads.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from services.operator_remedy import DeploymentPosture, config_key, remedy, worker_setting
from services.validation.checks import CONFIRM_AGAIN, Check, run_checks
from services.validation.dependencies import (
    WORKFLOW_POLLERS,
    check_schema,
    model_path_fix,
    poller_count,
    temporal_failure,
)
from services.validation.report import (
    MODEL_PATH,
    SCHEMA,
    TEMPORAL,
    CheckResult,
    Scope,
    TenantReport,
    dependencies,
    failed,
    failure_alert,
    passed,
    render_tenant_report,
    skipped,
)

log = logging.getLogger(__name__)

BUNDLE = "Tenant bundle"
SLACK_AUTH = "Slack auth"
SLACK_POST = "Slack post"
TICKET_STORE = "Ticket store"
TICKET_STORE_SYNC = "Ticket store sync"
SOURCE_CONTROL = "Source control"
GITHUB_TIMEOUT_SECONDS = 10.0
SLACK_TIMEOUT_SECONDS = 10
TOKEN_EXPIRY_WARN = timedelta(days=7)
GITHUB_EXPIRY_HEADER = "github-authentication-token-expiration"


@dataclass
class TenantValidationRequest:
    """What a tenant validation does beyond its checks.

    ``post`` posts the report to the tenant's Slack channel, and that post is the Slack post
    check. ``alert_failures`` sends the failing checks to the tenant's escalation channels.
    ``platform_failures`` names the dependencies the platform validation found failing.
    """

    post: bool = False
    alert_failures: bool = False
    platform_failures: list[str] = field(default_factory=list)


@dataclass
class TenantContext:
    settings: Any
    client: Any = None
    transport: httpx.AsyncBaseTransport | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def tenant(self) -> str:
        return self.settings.tenant.strip()


def _restart_worker(tenant: str) -> str:
    return f"`kubectl -n tenant-{tenant} rollout restart deploy/activity-worker`"


def _secret_fix(tenant: str, setting: str, what: str) -> tuple[Any, Any]:
    host = remedy("Set ", worker_setting(setting), f" to {what}.")
    deployed = remedy(
        f"Write {what} to the tenant's `{setting.lower()}` secret in the secret store, then "
        f"restart the tenant's activity worker with {_restart_worker(tenant)}."
    )
    return host.for_tenant(tenant), deployed


async def check_bundle(context: TenantContext) -> list[CheckResult]:
    from services.integration.credentials import ConfigError
    from services.integration.tenant_config import active_tenant_config, validate_bundle

    tenant = context.tenant
    fix = remedy(
        f"Correct the files the cause names in `config/local/tenants/{tenant}/`, check them with "
        f"`inv tenant-config-validate --tenant {tenant}`, and apply them with `inv k8s-reload`."
    )
    deployed = remedy(
        "Correct the files the cause names in the tenant's config repository and publish the "
        "bundle from its main branch. The worker activates the next valid bundle at its next "
        "refresh."
    )
    config = active_tenant_config()
    if config is None or config.active is None or config.active_path is None:
        return [
            failed(
                BUNDLE,
                "the worker serves no tenant config bundle",
                fix,
                fix_deployed=deployed,
                confirm=CONFIRM_AGAIN,
            )
        ]
    active = config.active
    try:
        await asyncio.to_thread(validate_bundle, config.active_path, context.settings)
    except ConfigError as exc:
        cause = f"the active bundle {active.id} is refused. {exc}"
        return [failed(BUNDLE, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)]
    latest = await asyncio.to_thread(config.source.latest)
    if latest.files and latest.id != active.id:
        refusal = await asyncio.to_thread(_refusal, config, latest, context.settings)
        if refusal:
            cause = (
                f"the latest published bundle {latest.id} is refused, so the worker keeps "
                f"bundle {active.id}. {refusal}"
            )
            return [failed(BUNDLE, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)]
        return [passed(BUNDLE, f"bundle {latest.id} is valid, and the next refresh activates it")]
    return [passed(BUNDLE, f"bundle {active.id} is active and valid")]


def _refusal(config: Any, identity: Any, settings: Any) -> str:
    from services.integration.credentials import ConfigError
    from services.integration.tenant_config import validate_bundle

    bundle = config.source.fetch(identity)
    with tempfile.TemporaryDirectory(prefix="tenant-validation-") as staging:
        for name, text in bundle.files.items():
            (Path(staging) / name).write_text(text, encoding="utf-8")
        try:
            validate_bundle(Path(staging), settings)
        except ConfigError as exc:
            return str(exc)
    return ""


def _escalation(settings: Any) -> Any:
    from services.integration.slack.shared_app import process_shared_app
    from services.orchestration.escalation import load_escalation_config

    return load_escalation_config(settings, shared_app=process_shared_app())


async def check_slack(context: TenantContext) -> list[CheckResult]:
    from services.integration.credentials import ConfigError
    from services.orchestration.escalation import SHARED_APP, SLACK

    tenant = context.tenant
    try:
        config = await asyncio.to_thread(_escalation, context.settings)
    except ConfigError as exc:
        return [
            failed(
                SLACK_AUTH,
                f"escalation.yaml is unusable. {exc}",
                remedy("Correct `escalation.yaml` as the cause names."),
                confirm=CONFIRM_AGAIN,
            )
        ]
    slack = config.slack
    if SLACK not in config.channels:
        return [skipped(SLACK_AUTH, "escalation.yaml sends no alert to Slack")]
    if slack is None:
        reason = (
            "alerts post through SLACK_WEBHOOK_URL, which offers no auth check"
            if context.settings.slack_webhook_url
            else "escalation.yaml names no Slack app"
        )
        return [skipped(SLACK_AUTH, reason)]
    if slack.app == SHARED_APP:
        return [
            skipped(
                SLACK_AUTH,
                "the tenant posts through the shared app, whose bot token only the posting "
                "service holds. A posted report checks it.",
            )
        ]
    fix, deployed = _secret_fix(tenant, "SLACK_BOT_TOKEN", "the bot token of the tenant's app")
    token = context.settings.slack_bot_token
    if not token:
        cause = "SLACK_BOT_TOKEN is not set, so the worker cannot post the tenant's alerts"
        return [failed(SLACK_AUTH, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)]
    try:
        answer = await asyncio.to_thread(_auth_test, token, slack.api_base_url)
    except Exception as exc:  # noqa: BLE001
        cause = f"Slack refused auth.test at {slack.api_base_url} with {_slack_error(exc)}"
        return [failed(SLACK_AUTH, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)]
    return [
        passed(
            SLACK_AUTH,
            f"the bot token authenticates as {answer.get('user', 'the bot')} in workspace "
            f"{answer.get('team', answer.get('team_id', ''))}",
        )
    ]


def _auth_test(token: str, base_url: str) -> dict[str, Any]:
    from services.integration.slack.client import tenant_web_client

    client = tenant_web_client(token, base_url, timeout=SLACK_TIMEOUT_SECONDS, retry_handlers=[])
    return dict(client.auth_test().data)


def _slack_error(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    error = response.get("error") if response is not None else None
    return str(error) if error else type(exc).__name__


async def check_ticket_store(context: TenantContext) -> list[CheckResult]:
    from services.integration.tickets.backend import REBAR
    from services.integration.tickets.factory import resolve_ticket_backend_name

    if resolve_ticket_backend_name(context.settings) == REBAR:
        results = [await asyncio.to_thread(_rebar_result, context.tenant)]
        sync = await asyncio.to_thread(_rebar_sync_result, context.tenant)
        return [*results, sync] if sync is not None else results
    return await asyncio.to_thread(_jira_results, context.tenant)


def _rebar_sync_result(tenant: str) -> CheckResult | None:
    """Compare the store's tickets branch with the bucket, for a store whose remote is on S3.

    rebar fetches before a read and pushes after a write on a best-effort basis, so a store
    that stops converging with its bucket shows only as tickets intake never sees.
    """
    from services import process
    from services.integration.tickets.rebar_config import load_rebar_backend_config
    from services.integration.tickets.store_remote import read_store_remote

    root = load_rebar_backend_config().repo_root
    if not (Path(root) / ".git").exists():
        return None
    remote = read_store_remote(root)
    if remote is None or not remote.is_s3:
        return None
    fix = remedy(
        "Resolve the cause, then validate again. A store that stays ahead or behind after "
        "the tenant's activity worker restarts has a history the bucket does not converge with."
    )

    def git(*args: str) -> process.CompletedProcess[str]:
        return process.run(
            ["git", "-C", root, *args],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )

    fetched = git("fetch", "--quiet", remote.name, "tickets")
    if fetched.returncode != 0:
        detail = fetched.stderr.strip()[-400:] or "no output"
        cause = f"fetching tickets from {remote.name} failed: {detail}"
        return failed(TICKET_STORE_SYNC, cause, fix, confirm=CONFIRM_AGAIN)
    counted = git(
        "rev-list", "--left-right", "--count", f"tickets...refs/remotes/{remote.name}/tickets"
    )
    if counted.returncode != 0:
        cause = f"comparing tickets with {remote.name} failed: {counted.stderr.strip()[-400:]}"
        return failed(TICKET_STORE_SYNC, cause, fix, confirm=CONFIRM_AGAIN)
    ahead, behind = (int(part) for part in counted.stdout.split())
    roots = git("rev-list", "--max-parents=0", "tickets").stdout.split()
    bucket_roots = git("rev-list", "--max-parents=0", f"refs/remotes/{remote.name}/tickets")
    if set(roots) - set(bucket_roots.stdout.split()):
        cause = (
            f"the store's tickets branch starts from a root commit {remote.name} does not hold, "
            "so the store keeps a history of its own that the bucket never receives, and ticket "
            "changes made through the bucket may not reach intake"
        )
        return failed(
            TICKET_STORE_SYNC,
            cause,
            _reseed_fix(tenant, root, posture_context=""),
            fix_deployed=_reseed_fix(tenant, root, posture_context=" --context <EKS context>"),
            confirm=CONFIRM_AGAIN,
        )
    if ahead or behind:
        cause = (
            f"the store's tickets branch is {ahead} commits ahead of and {behind} commits behind "
            f"{remote.name}, so ticket writes have not reached the bucket or bucket changes have "
            "not reached intake"
        )
        return failed(TICKET_STORE_SYNC, cause, fix, confirm=CONFIRM_AGAIN)
    return passed(TICKET_STORE_SYNC, f"the store's tickets branch matches {remote.name}")


def _reseed_fix(tenant: str, root: str, *, posture_context: str) -> Any:
    namespace = f"tenant-{tenant}"
    return remedy(
        "Move the store aside with "
        f"`kubectl{posture_context} -n {namespace} exec deploy/activity-worker -c activity-worker "
        f"-- mv {root} {root}.diverged`, then restart the tenant's activity worker with "
        f"`kubectl{posture_context} -n {namespace} rollout restart deploy/activity-worker`, "
        "which seeds a new store from the bucket. Ticket changes made only in the moved store "
        "stay in it."
    )


def _rebar_result(tenant: str) -> CheckResult:
    import rebar

    from services.integration.credentials import ConfigError
    from services.integration.tickets.rebar_config import load_rebar_backend_config
    from services.integration.tickets.store_config import load_tickets_store_config
    from services.integration.tickets.store_remote import check_store_remote, read_store_remote
    from services.orchestration.rebar_intake import POLL_REFUSED_CAUSE

    fix = remedy(
        "Correct `backend.repo_root` or `store` in `tickets.yaml` as the cause names, then "
        "restart the tenant's activity worker with `inv k8s-reload`, which seeds an absent store."
    )
    deployed = remedy(
        "Correct `backend.repo_root` or `store` in the tenant's `tickets.yaml` as the cause names "
        "and publish the bundle, then restart the tenant's activity worker with "
        f"{_restart_worker(tenant)}."
    )
    root = load_rebar_backend_config().repo_root
    if not (Path(root) / ".git").exists():
        cause = f"the rebar store at {root} holds no git repository"
        return failed(TICKET_STORE, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)
    remote = read_store_remote(root)
    if remote is not None and remote.is_s3:
        try:
            check_store_remote(remote, load_tickets_store_config())
        except ConfigError as exc:
            return failed(TICKET_STORE, str(exc), fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)
    try:
        tickets = rebar.list_tickets(full=False, repo_root=root)
    except Exception as exc:  # noqa: BLE001
        cause = f"the rebar store at {root} is unreadable ({type(exc).__name__}). {exc}"
        return failed(TICKET_STORE, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)
    if not _poll_authorized(root):
        identity_fix = remedy(
            "Set `AUTHORSHIP_IDENTITY_ID` to an identity whose email `poll.identity_roles` maps "
            "to a reader role, then restart the tenant's activity worker with `inv k8s-reload`."
        )
        identity_deployed = remedy(
            "Set `AUTHORSHIP_IDENTITY_ID` in the tenant's settings to an identity whose email "
            "`poll.identity_roles` maps to a reader role, then restart the tenant's activity "
            f"worker with {_restart_worker(tenant)}."
        )
        return failed(
            TICKET_STORE,
            POLL_REFUSED_CAUSE,
            identity_fix,
            fix_deployed=identity_deployed,
            confirm=CONFIRM_AGAIN,
        )
    return passed(TICKET_STORE, f"the rebar store at {root} reads {len(tickets)} tickets")


def _poll_authorized(root: str) -> bool:
    from scripts.rebar_poll import RebarIdentityAccess

    from services.integration.tickets.rebar_config import load_poll_config

    poll = load_poll_config()
    access = RebarIdentityAccess(root, reader_roles=poll.reader_roles, identity_roles=poll)
    return access.authorize().authorized


def _jira_results(tenant: str) -> list[CheckResult]:
    from services.integration.credentials import ConfigError, SecretNotFoundError
    from services.integration.jira.adapter import build_jira_adapter
    from services.integration.jira.credentials import build_jira_credential_resolver

    name = TICKET_STORE
    config_fix = remedy("Correct `jira.yaml` as the cause names.")
    try:
        resolver = build_jira_credential_resolver()
        projects = sorted(resolver.projects())
    except ConfigError as exc:
        return [failed(name, f"jira.yaml is unusable. {exc}", config_fix, confirm=CONFIRM_AGAIN)]
    if not projects:
        cause = "jira.yaml names no allowlisted project with a site, a token, and a board"
        return [failed(name, cause, config_fix, confirm=CONFIRM_AGAIN)]
    results = []
    for project in projects:
        check = f"{name} {project}"
        try:
            adapter = build_jira_adapter(project=project, resolver=resolver)
            adapter.client.search_issues(f'project = "{project}"', start_at=0, max_results=1)
        except SecretNotFoundError as exc:
            setting = str(exc.args[0])
            fix, deployed = _secret_fix(tenant, setting, "the Jira token of the project's site")
            cause = f"the Jira token {setting} has no value"
            results.append(failed(check, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN))
        except Exception as exc:  # noqa: BLE001
            setting = _jira_token_ref(project)
            fix, deployed = _secret_fix(
                tenant, setting, "a Jira token that reads the project, with its site's email"
            )
            cause = f"Jira refused a search of project {project}. {exc}"
            results.append(failed(check, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN))
        else:
            results.append(passed(check, f"the Jira token reads project {project}"))
    return results


def _jira_token_ref(project: str) -> str:
    from services.integration.credentials import load_env_config

    raw = load_env_config("jira") or {}
    override = ((raw.get("projects") or {}).get(project) or {}).get("token_ref")
    return str(override or (raw.get("default") or {}).get("token_ref") or "JIRA_API_TOKEN")


async def check_source_control(context: TenantContext) -> list[CheckResult]:
    from services.integration.repositories import GITHUB, LOCAL, load_repository_registry

    registry = await asyncio.to_thread(load_repository_registry, context.settings)
    results = [_local_result(registry, name) for name in sorted(registry.names(LOCAL))]
    github = sorted(registry.names(GITHUB))
    if github:
        results += await _github_results(context, registry, github)
    if not results:
        return [skipped(SOURCE_CONTROL, "repositories.yaml names no repository")]
    return results


def _local_result(registry: Any, name: str) -> CheckResult:
    location = Path(registry.location(name))
    check = f"{SOURCE_CONTROL} {name}"
    if (location / ".git").exists():
        return passed(check, f"the local checkout is at {location}")
    return failed(
        check,
        f"the local repository has no checkout at {location}",
        remedy("Copy the tenant's local repositories into its worker with `inv repos-sync`."),
        confirm=CONFIRM_AGAIN,
    )


def _token_ref(registry: Any, name: str) -> str:
    from services.integration.credentials import load_env_config

    repository = registry.get(name)
    if repository is not None and repository.token_ref:
        return repository.token_ref
    default = (load_env_config("github") or {}).get("default") or {}
    return str(default.get("token_ref") or "GITHUB_TOKEN")


async def _github_results(context: TenantContext, registry: Any, names: list[str]) -> list:
    from services.integration.credentials import ConfigError, SecretNotFoundError
    from services.integration.github.credentials import build_github_credential_resolver

    try:
        resolver = await asyncio.to_thread(build_github_credential_resolver, context.settings)
    except ConfigError as exc:
        cause = f"github.yaml is unusable. {exc}"
        fix = remedy("Correct `github.yaml` as the cause names.")
        return [failed(SOURCE_CONTROL, cause, fix, confirm=CONFIRM_AGAIN)]
    checks: list[Callable[[], Awaitable[CheckResult]]] = []
    async with httpx.AsyncClient(
        timeout=GITHUB_TIMEOUT_SECONDS, transport=context.transport
    ) as client:
        for name in names:
            check = f"{SOURCE_CONTROL} {name}"
            setting = _token_ref(registry, name)
            fix, deployed = _secret_fix(
                context.tenant, setting, "a token that reads the repository"
            )
            try:
                credential = resolver.resolve(name)
            except (SecretNotFoundError, RuntimeError) as exc:
                cause = f"the GitHub token of {name} is not configured ({exc})"
                unconfigured = failed(
                    check, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN
                )
                checks.append(partial(_ready, unconfigured))
                continue
            if credential is None:
                continue
            checks.append(
                partial(_github_result, client, check, credential, name, context.now, fix, deployed)
            )
        return list(await asyncio.gather(*(run() for run in checks)))


async def _ready(result: CheckResult) -> CheckResult:
    return result


async def _github_result(
    client: httpx.AsyncClient,
    check: str,
    credential: Any,
    name: str,
    now: datetime,
    fix: Any,
    deployed: Any,
) -> CheckResult:
    target = f"{credential.owner}/{name}"
    url = f"{credential.api_url.rstrip('/')}/repos/{target}"
    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        cause = f"GitHub at {credential.api_url} is unreachable ({type(exc).__name__})"
        return failed(
            check,
            cause,
            remedy("Check the worker's HTTPS egress to the GitHub host `github.yaml` names."),
            confirm=CONFIRM_AGAIN,
        )
    if response.status_code != 200:
        cause = f"GitHub answered {response.status_code} to a read of {target} with the token"
        return failed(check, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)
    expiry = _token_expiry(response.headers.get(GITHUB_EXPIRY_HEADER, ""))
    if expiry is None:
        return passed(check, f"the token reads {target}")
    stamp = expiry.strftime("%Y-%m-%d %H:%M UTC")
    if expiry - now <= TOKEN_EXPIRY_WARN:
        cause = f"the token reads {target} and expires at {stamp}"
        return failed(check, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)
    return passed(check, f"the token reads {target} and expires at {stamp}")


def _token_expiry(value: str) -> datetime | None:
    text = value.strip()
    for layout in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S UTC"):
        try:
            parsed = datetime.strptime(text, layout)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


async def check_model_path(context: TenantContext) -> list[CheckResult]:
    from services.orchestration.model_path import probe_model_path

    health = await probe_model_path(context.settings, transport=context.transport)
    if health.healthy:
        return [passed(MODEL_PATH, "a one-token call through LiteLLM answered")]
    fix, deployed = model_path_fix(health.hop)
    cause = f"the {health.hop} hop failed. {health.error}"
    return [failed(MODEL_PATH, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)]


async def check_temporal(context: TenantContext) -> list[CheckResult]:
    from services.orchestration.task_queues import run_task_queue

    if context.client is None:
        return [skipped(TEMPORAL, "the validation runs without a Temporal client")]
    queue = run_task_queue(context.settings)
    try:
        count = await poller_count(context.client, queue, WORKFLOW_POLLERS)
    except Exception as exc:  # noqa: BLE001
        return [temporal_failure(TEMPORAL, f"Temporal did not describe {queue} ({exc})")]
    if not count:
        return [temporal_failure(TEMPORAL, f"no workflow worker polls {queue}")]
    return [passed(TEMPORAL, f"{count} workflow workers poll {queue}")]


TENANT_CHECKS: tuple[Check[TenantContext], ...] = (
    Check(BUNDLE, Scope.TENANT, check_bundle),
    Check(SLACK_AUTH, Scope.TENANT, check_slack),
    Check(TICKET_STORE, Scope.TENANT, check_ticket_store),
    Check(SOURCE_CONTROL, Scope.TENANT, check_source_control),
    Check(MODEL_PATH, Scope.PLATFORM, check_model_path, dependency=MODEL_PATH),
    Check(TEMPORAL, Scope.PLATFORM, check_temporal, dependency=TEMPORAL),
    Check(SCHEMA, Scope.PLATFORM, check_schema, dependency=SCHEMA),
)


def posture(settings: Any) -> DeploymentPosture:
    return DeploymentPosture(settings.deployment_posture)


async def validate_tenant(
    settings: Any,
    request: TenantValidationRequest,
    *,
    client: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
    checks: tuple[Check[TenantContext], ...] = TENANT_CHECKS,
) -> TenantReport:
    context = TenantContext(settings, client=client, transport=transport)
    results = await run_checks(checks, context)
    for result in results:
        if result.scope == Scope.PLATFORM and result.failed:
            log.warning("validation dependency %s failed: %s", result.name, result.cause)
    report = TenantReport(
        tenant=context.tenant,
        checks=[result for result in results if result.scope == Scope.TENANT],
        dependencies=dependencies(
            [result for result in results if result.scope == Scope.PLATFORM],
            request.platform_failures,
        ),
    )
    if request.post:
        report = await asyncio.to_thread(post_report, report, settings)
    if request.alert_failures and report.failed:
        await asyncio.to_thread(alert_failures, report, settings)
    return report


def post_report(report: TenantReport, settings: Any) -> TenantReport:
    """Post the report to the tenant's Slack channel, counting the post as the Slack post check."""
    from services.orchestration.notifications import AlertMessage, SlackChannel

    posted = replace(report, checks=[*report.checks, passed(SLACK_POST, "this report is the post")])
    message = AlertMessage(
        subject=f"Deployment validation of tenant {report.tenant}",
        body=render_tenant_report(posted, posture(settings)),
        issue_key="",
    )
    try:
        delivered = SlackChannel().deliver(message, settings, _escalation(settings))
    except Exception as exc:  # noqa: BLE001
        cause = f"the report post failed with {_slack_error(exc)}"
    else:
        if delivered:
            return posted
        cause = "the worker has no Slack app, webhook, or posting service to post the report with"
    result = failed(
        SLACK_POST,
        cause,
        remedy(
            "Invite the app to the channel ",
            config_key("escalation", "slack.channel"),
            " names, or correct the channel.",
        ).for_tenant(report.tenant),
        confirm=CONFIRM_AGAIN,
    )
    return replace(report, checks=[*report.checks, result])


def alert_failures(report: TenantReport, settings: Any) -> list[str]:
    from services.orchestration.notifications import AlertMessage, deliver_alert

    subject, body = failure_alert(report, posture(settings))
    return deliver_alert(AlertMessage(subject=subject, body=body, issue_key=""), settings)
