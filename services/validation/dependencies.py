"""The platform dependencies both the tenant and the platform validation check."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from services.operator_remedy import Remedy, remedy, restore_model_path
from services.validation.checks import CONFIRM_AGAIN
from services.validation.report import SCHEMA, TEMPORAL, CheckResult, Scope, failed, passed

log = logging.getLogger(__name__)

_DEPLOYED_MODEL_PATH_FIXES = {
    "credentials": (
        "Re-apply the composition root with `terraform apply`, which restores the `litellm` Pod "
        "Identity association and the Bedrock policy of its role."
    ),
    "bedrock_path": (
        "Re-apply the composition root with `terraform apply`, which restores the Bedrock "
        "runtime interface endpoint and the network policy that admits the HTTPS egress of "
        "LiteLLM."
    ),
    "litellm": (
        "Check LiteLLM with `kubectl -n agentic-dev-platform rollout status deploy/litellm`, "
        "and redeploy it with `inv eks-deploy` when it is not ready."
    ),
}


def model_path_fix(hop: str) -> tuple[Remedy, Remedy]:
    """Return the host fix and the deployed fix for a failing hop of the model path.

    The deployed fix re-applies the declared configuration, because a deployed fault on the model
    path is configuration drift rather than a session to renew.
    """
    deployed = _DEPLOYED_MODEL_PATH_FIXES.get(hop, _DEPLOYED_MODEL_PATH_FIXES["litellm"])
    return remedy("Run ", restore_model_path(hop), "."), remedy(deployed)


def database_url(settings: Any) -> str:
    from scripts.config import to_host_url

    return to_host_url(settings.database_url, settings)


async def schema_result(settings: Any, name: str = SCHEMA) -> CheckResult:
    """Compare the applied migrations with the head this image carries, as the schema guard does."""
    from database.schema_guard import SchemaState, applied_revisions, classify, migration_script

    script = migration_script()
    heads = ", ".join(sorted(script.get_heads()))
    try:
        applied = await applied_revisions(database_url(settings))
    except Exception as exc:  # noqa: BLE001
        return failed(
            name,
            f"the database did not answer ({type(exc).__name__}). {exc}",
            remedy(
                "Check that Postgres is up with `docker compose ps postgres`, and that "
                "`DATABASE_URL` names the host and port it listens on."
            ),
            fix_deployed=remedy(
                "Check the status of the Aurora writer and the value of the "
                "`database_platform_app` secret the platform pods mount."
            ),
            confirm=CONFIRM_AGAIN,
        )
    state = classify(applied, script)
    current = ", ".join(sorted(applied)) or "no revision"
    if state is SchemaState.CURRENT:
        return passed(name, f"the schema is at head {heads}")
    if state is SchemaState.UNKNOWN:
        return failed(
            name,
            f"the database schema is at {current}, which this image does not carry. Its head is "
            f"{heads}.",
            remedy("Deploy the build that migrated the database, or restore it from a backup."),
            confirm=CONFIRM_AGAIN,
        )
    return failed(
        name,
        f"the database schema is at {current}, and this image expects {heads}",
        remedy("Run `inv db-migrate`."),
        fix_deployed=remedy(
            "Run `inv platform-deploy`, which runs db-migrate before it starts the services."
        ),
        confirm=CONFIRM_AGAIN,
    )


class _SettingsContext(Protocol):
    @property
    def settings(self) -> Any: ...


async def check_schema(context: _SettingsContext) -> list[CheckResult]:
    return [await schema_result(context.settings)]


WORKFLOW_POLLERS = "workflow"
ACTIVITY_POLLERS = "activity"


async def poller_count(client: Any, queue: str, kind: str) -> int:
    """Return how many workers polled the queue for workflow or activity tasks recently."""
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

    queue_type = (
        TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
        if kind == WORKFLOW_POLLERS
        else TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY
    )
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=client.namespace,
            task_queue=TaskQueue(name=queue),
            task_queue_type=queue_type,
        )
    )
    return len(response.pollers)


def temporal_failure(name: str, cause: str, *, scope: str = Scope.PLATFORM) -> CheckResult:
    return failed(
        name,
        cause,
        remedy(
            "Start the workflow worker and the tenant's activity worker with `inv k8s-reload`, "
            "and check Temporal with `docker compose ps temporal`."
        ),
        fix_deployed=remedy(
            "Run `inv platform-deploy` for the workflow worker and `inv eks-deploy` for the "
            "tenant's activity worker, then check that each is ready."
        ),
        confirm=CONFIRM_AGAIN,
        scope=scope,
        dependency=TEMPORAL,
    )
