"""Temporal Worker Deployment registration for the platform workers.

Worker Versioning V1, the version sets behind `update_worker_build_id_compatibility`, is
deprecated from Temporal server 1.28.0, sunset in 1.31.0, and removed in 1.33, and temporalio
1.34.0 marks `Worker(build_id=..., use_worker_versioning=True)` deprecated in its favour.
Worker Deployments replace it. A deployment version spans every task queue its workers poll,
so a `PINNED` execution keeps its activities on the version that started it even when they run
on another queue.
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.api.deployment.v1 import WorkerDeploymentVersion as DeploymentVersionProto
from temporalio.api.enums.v1 import VersionDrainageStatus
from temporalio.api.workflowservice.v1 import (
    DeleteWorkerDeploymentVersionRequest,
    DescribeWorkerDeploymentRequest,
    DescribeWorkerDeploymentVersionRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.service import RPCError
from temporalio.worker import WorkerDeploymentConfig

from services.orchestration.suspension import is_newer_build

log = logging.getLogger("worker-versioning")

POLLER_WAIT_SECONDS = 120.0
POLLER_POLL_INTERVAL_SECONDS = 2.0
CONFLICT_RETRIES = 5


def deployment_config(settings) -> WorkerDeploymentConfig:
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name=settings.worker_deployment_name,
            build_id=settings.agent_build_id,
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


async def _describe_deployment(client: Client, deployment_name: str):
    return await client.workflow_service.describe_worker_deployment(
        DescribeWorkerDeploymentRequest(namespace=client.namespace, deployment_name=deployment_name)
    )


def _current_build_id(described) -> str:
    return described.worker_deployment_info.routing_config.current_deployment_version.build_id


async def _await_version_pollers(
    client: Client,
    settings,
    task_queues: list[str],
    timeout: float,
) -> None:
    """Wait until the server lists this version on every task queue this process polls.

    A version registers when one of its workers polls, so setting it current before then is
    refused. Each worker process waits for its own queues, and the deployment version
    accumulates the queues of every process that shares the build identifier.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    version = DeploymentVersionProto(
        deployment_name=settings.worker_deployment_name, build_id=settings.agent_build_id
    )
    wanted = set(task_queues)
    while True:
        try:
            described = await client.workflow_service.describe_worker_deployment_version(
                DescribeWorkerDeploymentVersionRequest(
                    namespace=client.namespace, deployment_version=version
                )
            )
            listed = {
                info.name for info in described.worker_deployment_version_info.task_queue_infos
            }
            if wanted.issubset(listed):
                return
        except RPCError as exc:
            last = exc
            log.debug("deployment version not registered yet: %s", exc)
        else:
            last = None
        if asyncio.get_running_loop().time() >= deadline:
            raise SystemExit(
                f"build {settings.agent_build_id!r} of worker deployment "
                f"{settings.worker_deployment_name!r} did not register task queues "
                f"{sorted(wanted)} within {timeout:.0f}s, so the server cannot route work to "
                f"it. Check that the Temporal server supports Worker Deployments, or set "
                f"WORKER_VERSIONING_ENABLED=false to run unversioned."
                + (f" Server reported: {last}" if last is not None else "")
            )
        await asyncio.sleep(POLLER_POLL_INTERVAL_SECONDS)


async def set_current_version(
    client: Client,
    settings,
    task_queues: list[str],
    *,
    poller_timeout: float = POLLER_WAIT_SECONDS,
) -> None:
    """Route new executions to this build once its workers poll, leaving older ones draining.

    A worker restarted on a superseded build keeps draining the executions pinned to it rather
    than taking routing back from a newer build, so the current version only moves forward.
    """
    deployment_name = settings.worker_deployment_name
    build_id = settings.agent_build_id
    await _await_version_pollers(client, settings, task_queues, poller_timeout)

    for _ in range(CONFLICT_RETRIES):
        described = await _describe_deployment(client, deployment_name)
        current = _current_build_id(described)
        if current == build_id:
            log.info("build %s is already the current version of %s", build_id, deployment_name)
            return
        if current and not is_newer_build(build_id, current):
            log.info(
                "leaving build %s current on %s, which outranks this build %s",
                current,
                deployment_name,
                build_id,
            )
            return
        try:
            await client.workflow_service.set_worker_deployment_current_version(
                SetWorkerDeploymentCurrentVersionRequest(
                    namespace=client.namespace,
                    deployment_name=deployment_name,
                    build_id=build_id,
                    conflict_token=described.conflict_token,
                    identity=f"worker-{build_id}",
                    # The worker services share a build but poll different queues and start
                    # independently, so the first one to reach this point covers only its own.
                    ignore_missing_task_queues=True,
                )
            )
        except RPCError as exc:
            if _current_build_id(await _describe_deployment(client, deployment_name)) == build_id:
                log.info(
                    "build %s became current on %s while this worker was setting it",
                    build_id,
                    deployment_name,
                )
                return
            if _is_conflict(exc):
                continue
            raise SystemExit(
                f"could not make build {build_id!r} the current version of worker deployment "
                f"{deployment_name!r}, so the server would route new executions to another "
                f"build. Check that the Temporal server supports Worker Deployments, prune "
                f"drained versions with `inv prune-worker-versions` when the deployment has "
                f"reached its version ceiling, or set WORKER_VERSIONING_ENABLED=false to run "
                f"unversioned. Server reported: {exc}"
            ) from exc
        else:
            log.info("build %s is the current version of %s", build_id, deployment_name)
            return

    raise SystemExit(
        f"could not make build {build_id!r} the current version of worker deployment "
        f"{deployment_name!r} after {CONFLICT_RETRIES} attempts, because another worker kept "
        f"changing it first."
    )


def _is_conflict(exc: RPCError) -> bool:
    return "conflict token" in str(exc).lower()


async def prune_drained_versions(client: Client, settings) -> tuple[list[str], list[str]]:
    """Delete every drained version of the deployment, returning the deleted and kept builds.

    A version the server keeps counts against the deployment version ceiling, and a drained
    version has no execution left that could be pinned to it. The server refuses to delete a
    version whose task queues still have pollers, which happens when a worker on that build is
    still running, so one refusal leaves the remaining versions to the rest of the pass.
    """
    described = await _describe_deployment(client, settings.worker_deployment_name)
    deleted: list[str] = []
    kept: list[str] = []
    for summary in described.worker_deployment_info.version_summaries:
        status = summary.drainage_info.status or summary.drainage_status
        if status != VersionDrainageStatus.VERSION_DRAINAGE_STATUS_DRAINED:
            continue
        build_id = summary.deployment_version.build_id
        try:
            await client.workflow_service.delete_worker_deployment_version(
                DeleteWorkerDeploymentVersionRequest(
                    namespace=client.namespace,
                    deployment_version=summary.deployment_version,
                    identity="prune-worker-versions",
                )
            )
        except RPCError as exc:
            kept.append(build_id)
            log.warning(
                "kept drained build %s of worker deployment %s, server reported: %s",
                build_id,
                settings.worker_deployment_name,
                exc,
            )
            continue
        deleted.append(build_id)
        log.info(
            "deleted drained build %s of worker deployment %s",
            build_id,
            settings.worker_deployment_name,
        )
    return deleted, kept
