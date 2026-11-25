"""Platform validation, which runs as the deployment-validate Job in the platform namespace.

The Job holds the platform's access. It checks each model alias, the AWS identity LiteLLM signs
Bedrock calls with, the database schema, Temporal, and each tenant's task queue, then starts the
validation of each requested tenant on that tenant's task queue, where the tenant's activity
worker runs it with the tenant's own credentials. The platform failures reach each tenant as
dependency names only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from services.operator_remedy import DeploymentPosture, remedy
from services.validation.checks import CONFIRM_AGAIN, Check, run_checks
from services.validation.dependencies import (
    ACTIVITY_POLLERS,
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
    PlatformReport,
    Scope,
    TenantReport,
    dependencies,
    failed,
    passed,
    skipped,
)
from services.validation.tenant import TenantValidationRequest, posture

log = logging.getLogger(__name__)

MODEL_ALIASES = "Model aliases"
AWS_IDENTITY = "AWS identity"
TASK_QUEUE = "Task queue"
TENANT_VALIDATION = "Tenant validation"
ALIAS_TIMEOUT_SECONDS = 30.0
TENANT_TIMEOUT = timedelta(minutes=5)
WORKFLOW_ID_PREFIX = "deployment-validate"


@dataclass
class PlatformContext:
    settings: Any
    tenants: list[str]
    client: Any = None
    temporal_error: str = ""
    transport: httpx.AsyncBaseTransport | None = None
    sts: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


def _litellm_url(settings: Any) -> str:
    from scripts.config import to_host_url

    return to_host_url(settings.litellm_base_url, settings)


def _hop_failure(name: str, hop: str, detail: str) -> CheckResult:
    from services.orchestration.notifications import model_error_summary
    from services.orchestration.remedies import MODEL_PATH_FAILURES

    fix, deployed = model_path_fix(hop)
    reason = MODEL_PATH_FAILURES.get(hop, "a hop of the model path failed")
    cause = f"the {hop} hop failed, because {reason}. {model_error_summary(detail)}"
    return failed(name, cause, fix, fix_deployed=deployed, confirm=CONFIRM_AGAIN)


async def _alias_result(client: httpx.AsyncClient, alias: str, headers: dict) -> CheckResult:
    from services.orchestration.model_path import (
        HOP_LITELLM,
        classify_gateway_answer,
        classify_transport_error,
    )

    name = f"Model alias {alias}"
    payload = {"model": alias, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    try:
        response = await client.post("/v1/chat/completions", json=payload, headers=headers)
    except httpx.TransportError as exc:
        hop = classify_transport_error(exc) or HOP_LITELLM
        return _hop_failure(name, hop, f"{type(exc).__name__} {exc}")
    if response.status_code < 400:
        return passed(name, "a one-token call answered")
    if response.status_code == 429:
        return skipped(name, "LiteLLM throttled the call, so the alias answer is unknown")
    hop = classify_gateway_answer(response.text)
    if hop is not None:
        return _hop_failure(name, hop, response.text)
    return failed(
        name,
        f"LiteLLM answered {response.status_code}. {response.text[:300]}",
        remedy("Correct the alias in `config/platform/litellm-config.yaml`, then reload LiteLLM."),
        fix_deployed=remedy(
            "Correct the alias in `config/platform/litellm-config.yaml`, then run `inv eks-deploy`."
        ),
        confirm=CONFIRM_AGAIN,
    )


async def check_model_aliases(context: PlatformContext) -> list[CheckResult]:
    from services.orchestration.model_path import HOP_LITELLM
    from services.orchestration.temporal_activities.bedrock import _litellm_auth_headers

    headers = _litellm_auth_headers(context.settings)
    base_url = _litellm_url(context.settings)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=ALIAS_TIMEOUT_SECONDS, transport=context.transport
    ) as client:
        try:
            response = await client.get("/v1/models", headers=headers)
        except httpx.TransportError as exc:
            detail = f"{base_url} {type(exc).__name__} {exc}"
            return [_hop_failure(MODEL_ALIASES, HOP_LITELLM, detail)]
        if response.status_code >= 400:
            return [
                failed(
                    MODEL_ALIASES,
                    f"LiteLLM answered {response.status_code} to the model list",
                    remedy("Check that `LITELLM_MASTER_KEY` matches the key LiteLLM runs with."),
                    confirm=CONFIRM_AGAIN,
                )
            ]
        aliases = sorted({str(entry["id"]) for entry in response.json().get("data") or []})
        if not aliases:
            return [
                failed(
                    MODEL_ALIASES,
                    "LiteLLM serves no model alias",
                    remedy("Restore `model_list` in `config/platform/litellm-config.yaml`."),
                    confirm=CONFIRM_AGAIN,
                )
            ]
        return list(
            await asyncio.gather(*(_alias_result(client, alias, headers) for alias in aliases))
        )


async def check_aws_identity(context: PlatformContext) -> list[CheckResult]:
    """Check the identity LiteLLM signs Bedrock calls with.

    Locally the Job mounts the session `inv aws-login` stored for LiteLLM, so it reads the
    session's expiry and asks STS whose it is. In EKS LiteLLM signs with its own Pod Identity
    role, which no other pod can assume, so a one-token call through LiteLLM shows whether
    Bedrock accepts it.
    """
    if context.settings.aws_credentials_file:
        return [await asyncio.to_thread(_session_result, context)]
    from services.orchestration.model_path import HOP_CREDENTIALS, HOP_LITELLM, probe_model_path

    health = await probe_model_path(context.settings, transport=context.transport)
    if health.hop == HOP_CREDENTIALS:
        return [_hop_failure(AWS_IDENTITY, HOP_CREDENTIALS, health.error)]
    if health.hop == HOP_LITELLM:
        return [skipped(AWS_IDENTITY, "LiteLLM did not answer, so its identity is unknown")]
    identity = (
        "its Pod Identity role"
        if context.settings.deployment_posture == DeploymentPosture.DEPLOYED
        else "its AWS credentials"
    )
    return [passed(AWS_IDENTITY, f"Bedrock accepted a call LiteLLM signed with {identity}")]


def _session_result(context: PlatformContext) -> CheckResult:
    from services.aws_credentials import expiry_report
    from services.orchestration.model_path import HOP_CREDENTIALS

    settings = context.settings
    path = Path(settings.aws_credentials_file)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        detail = (
            f"{path} is unreadable ({type(exc).__name__}), so the aws-credentials Secret holds "
            "no session"
        )
        return _hop_failure(AWS_IDENTITY, HOP_CREDENTIALS, detail)
    renew, report = expiry_report(record, settings.aws_credentials_warn_seconds, context.now)
    if renew:
        return _hop_failure(AWS_IDENTITY, HOP_CREDENTIALS, report)
    try:
        caller = (context.sts or partial(_caller_identity, region=settings.aws_region))(record)
    except Exception as exc:  # noqa: BLE001
        detail = f"STS refused the stored session ({type(exc).__name__}). {exc}"
        return _hop_failure(AWS_IDENTITY, HOP_CREDENTIALS, detail)
    arn = caller.get("Arn", "an unnamed principal")
    return passed(AWS_IDENTITY, f"LiteLLM signs as {arn}, and {report}")


def _caller_identity(record: dict[str, Any], *, region: str) -> dict[str, Any]:
    import boto3
    from scripts.aws_clients import configure_aws_fips

    configure_aws_fips()
    session = boto3.Session(
        aws_access_key_id=record.get("AccessKeyId"),
        aws_secret_access_key=record.get("SecretAccessKey"),
        aws_session_token=record.get("SessionToken"),
        region_name=region,
    )
    return dict(session.client("sts").get_caller_identity())


async def check_temporal(context: PlatformContext) -> list[CheckResult]:
    if context.client is None:
        return [temporal_failure(TEMPORAL, f"Temporal is unreachable. {context.temporal_error}")]
    from services.orchestration.task_queues import tenant_task_queue

    results = [passed(TEMPORAL, f"connected to namespace {context.client.namespace}")]
    kinds = (WORKFLOW_POLLERS, ACTIVITY_POLLERS)
    queues = [tenant_task_queue(context.settings, tenant) for tenant in context.tenants]
    answers = await asyncio.gather(
        *(poller_count(context.client, queue, kind) for queue in queues for kind in kinds),
        return_exceptions=True,
    )
    for index, queue in enumerate(queues):
        name = f"{TASK_QUEUE} {queue}"
        counts = dict(
            zip(kinds, answers[index * len(kinds) : (index + 1) * len(kinds)], strict=True)
        )
        error = next((count for count in counts.values() if isinstance(count, BaseException)), None)
        if isinstance(error, Exception):
            results.append(temporal_failure(name, f"Temporal did not describe {queue} ({error})"))
            continue
        if error is not None:
            raise error
        missing = [kind for kind, count in counts.items() if not count]
        if missing:
            cause = f"no {' or '.join(missing)} worker polls {queue}"
            results.append(temporal_failure(name, cause))
        else:
            results.append(passed(name, f"workflow and activity workers poll {queue}"))
    return results


PLATFORM_CHECKS: tuple[Check[PlatformContext], ...] = (
    Check(MODEL_ALIASES, Scope.PLATFORM, check_model_aliases, dependency=MODEL_PATH),
    Check(AWS_IDENTITY, Scope.PLATFORM, check_aws_identity, dependency=MODEL_PATH),
    Check(SCHEMA, Scope.PLATFORM, check_schema, dependency=SCHEMA),
    Check(TEMPORAL, Scope.PLATFORM, check_temporal, dependency=TEMPORAL),
)


def platform_failures(results: Sequence[CheckResult]) -> list[str]:
    """Return the dependency names a platform result failed, the only detail a tenant receives.

    A task queue result names one tenant's queue, so it is that tenant's own Temporal check, not
    a dependency of every tenant.
    """
    shared = [result for result in results if not result.name.startswith(TASK_QUEUE)]
    return [dependency.name for dependency in dependencies(shared) if not dependency.ok]


def _unreached(tenant: str, cause: str) -> TenantReport:
    return TenantReport(
        tenant=tenant,
        checks=[
            failed(
                TENANT_VALIDATION,
                cause,
                remedy(
                    "Check the tenant's activity worker with `kubectl -n tenant-"
                    f"{tenant} rollout status deploy/activity-worker`, and the workflow worker."
                ),
                confirm=CONFIRM_AGAIN,
            )
        ],
    )


async def tenant_report(
    client: Any, settings: Any, tenant: str, request: TenantValidationRequest
) -> TenantReport:
    """Run one tenant's validation on its task queue, or report why it did not finish."""
    from services.orchestration.task_queues import tenant_task_queue
    from services.orchestration.temporal_workflows.validation import TenantValidationWorkflow

    if client is None:
        return _unreached(tenant, "Temporal is unreachable, so the validation could not start")
    try:
        return await client.execute_workflow(
            TenantValidationWorkflow.run,
            request,
            id=f"{WORKFLOW_ID_PREFIX}-{tenant}-{uuid.uuid4().hex[:12]}",
            task_queue=tenant_task_queue(settings, tenant),
            execution_timeout=TENANT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("the validation of tenant %s did not finish", tenant, exc_info=True)
        cause = f"the validation did not finish ({type(exc).__name__}). {_root_cause(exc)}"
        return _unreached(tenant, cause)


def alert_platform_failures(report: PlatformReport, settings: Any) -> list[str]:
    """Send the failing platform checks with their fixes to the administrator destination, and
    return the destinations reached."""
    from services.orchestration.notifications import AlertMessage, deliver_admin_alert
    from services.validation.report import platform_failure_alert

    alert = platform_failure_alert(report, posture(settings))
    if alert is None:
        return []
    subject, body = alert
    return deliver_admin_alert(AlertMessage(subject=subject, body=body, issue_key=""), settings)


def _root_cause(error: BaseException) -> str:
    while error.__cause__ is not None:
        error = error.__cause__
    return str(error)


async def validate_deployment(
    settings: Any,
    *,
    tenants: Sequence[str],
    queues: Sequence[str] = (),
    request: TenantValidationRequest | None = None,
    client: Any = None,
    temporal_error: str = "",
    checks: tuple[Check[PlatformContext], ...] = PLATFORM_CHECKS,
    run_tenant: Callable[..., Any] = tenant_report,
) -> PlatformReport:
    """Run the platform checks, then the validation of each of ``tenants`` with its failures.

    ``queues`` names the tenants whose task queues the Temporal check describes, every tenant of
    the platform when the validation covers it as a whole.
    """
    context = PlatformContext(
        settings, list(queues or tenants), client=client, temporal_error=temporal_error
    )
    results = await run_checks(checks, context)
    base = request or TenantValidationRequest()
    tenant_request = TenantValidationRequest(
        post=base.post,
        alert_failures=base.alert_failures,
        platform_failures=platform_failures(results),
    )
    reports = await asyncio.gather(
        *(run_tenant(client, settings, tenant, tenant_request) for tenant in tenants)
    )
    return PlatformReport(checks=results, tenants=list(reports))
