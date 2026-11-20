"""Worker Deployment pinning against a Temporal dev server.

Covers what a recording double cannot. The workflow runs on one task queue and its review
activity on another, and a `PINNED` execution has to keep both on the version that started it
after a newer version becomes current.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta

import pytest
from services.orchestration.worker_versioning import (
    deployment_config,
    move_to_current_version,
    pinned_executions,
    set_current_version,
)
from temporalio import activity, workflow
from temporalio.api.enums.v1 import VersioningBehavior as VersioningBehaviorProto
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

MAIN_QUEUE = "versioning-main"
REVIEW_QUEUE = "versioning-review"
MAIN_ACTIVITY = "report_main_build"
REVIEW_ACTIVITY = "report_review_build"


@dataclass(frozen=True)
class _VersionSettings:
    agent_build_id: str
    worker_deployment_name: str
    worker_versioning_enabled: bool = True


@workflow.defn(name="BuildPinningWorkflow")
class BuildPinningWorkflow:
    def __init__(self) -> None:
        self._released = False

    @workflow.run
    async def run(self) -> dict[str, str]:
        await workflow.wait_condition(lambda: self._released)
        return {
            "workflow_queue": await workflow.execute_activity(
                MAIN_ACTIVITY, start_to_close_timeout=timedelta(seconds=30)
            ),
            "review_queue": await workflow.execute_activity(
                REVIEW_ACTIVITY,
                task_queue=REVIEW_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
            ),
        }

    @workflow.signal
    def release(self) -> None:
        self._released = True


def _reporter(name: str, build_id: str):
    @activity.defn(name=name)
    async def report() -> str:
        return build_id

    return report


def _workers(client: Client, settings: _VersionSettings) -> list[Worker]:
    config = deployment_config(settings)
    return [
        Worker(
            client,
            task_queue=MAIN_QUEUE,
            workflows=[BuildPinningWorkflow],
            activities=[_reporter(MAIN_ACTIVITY, settings.agent_build_id)],
            deployment_config=config,
        ),
        Worker(
            client,
            task_queue=REVIEW_QUEUE,
            activities=[_reporter(REVIEW_ACTIVITY, settings.agent_build_id)],
            deployment_config=config,
        ),
    ]


async def _deploy(client: Client, settings: _VersionSettings, stack) -> None:
    for worker in _workers(client, settings):
        await stack.enter_async_context(worker)
    await set_current_version(client, settings, [MAIN_QUEUE, REVIEW_QUEUE])


@pytest.mark.asyncio
async def test_a_pinned_execution_keeps_both_queues_on_the_build_that_started_it() -> None:
    deployment = f"versioning-test-{uuid.uuid4().hex[:8]}"
    first = _VersionSettings("0.3.0.20261028093000", deployment)
    second = _VersionSettings("0.3.0.20261029093000", deployment)

    async with await WorkflowEnvironment.start_local() as env, AsyncExitStack() as stack:
        await _deploy(env.client, first, stack)

        in_flight = await env.client.start_workflow(
            BuildPinningWorkflow.run, id=f"pinned-{uuid.uuid4().hex[:8]}", task_queue=MAIN_QUEUE
        )
        versioning = await _await_version_claim(in_flight)
        assert versioning.behavior == VersioningBehaviorProto.VERSIONING_BEHAVIOR_PINNED
        assert versioning.deployment_version.build_id == first.agent_build_id

        await _deploy(env.client, second, stack)

        await in_flight.signal(BuildPinningWorkflow.release)
        assert await in_flight.result() == {
            "workflow_queue": first.agent_build_id,
            "review_queue": first.agent_build_id,
        }

        started_after = await env.client.start_workflow(
            BuildPinningWorkflow.run, id=f"current-{uuid.uuid4().hex[:8]}", task_queue=MAIN_QUEUE
        )
        await started_after.signal(BuildPinningWorkflow.release)
        assert await started_after.result() == {
            "workflow_queue": second.agent_build_id,
            "review_queue": second.agent_build_id,
        }


@pytest.mark.asyncio
async def test_a_parked_execution_moves_to_the_current_build_before_it_is_resumed() -> None:
    deployment = f"versioning-move-{uuid.uuid4().hex[:8]}"
    parked_on = _VersionSettings("0.3.0.20261028093000", deployment)
    resumed_on = _VersionSettings("0.3.0.20261029093000", deployment)

    async with await WorkflowEnvironment.start_local() as env, AsyncExitStack() as stack:
        async with AsyncExitStack() as retired:
            await _deploy(env.client, parked_on, retired)
            parked = await env.client.start_workflow(
                BuildPinningWorkflow.run,
                id=f"parked-{uuid.uuid4().hex[:8]}",
                task_queue=MAIN_QUEUE,
            )
            claimed = await _await_version_claim(parked)
            assert claimed.deployment_version.build_id == parked_on.agent_build_id

        await _deploy(env.client, resumed_on, stack)
        assert await pinned_executions(env.client, deployment, parked_on.agent_build_id) == [
            parked.id
        ]
        await move_to_current_version(env.client, resumed_on, parked.id)
        assert await pinned_executions(env.client, deployment, parked_on.agent_build_id) == []

        await parked.signal(BuildPinningWorkflow.release)
        assert await parked.result() == {
            "workflow_queue": resumed_on.agent_build_id,
            "review_queue": resumed_on.agent_build_id,
        }


async def _await_version_claim(handle, timeout: float = 30.0):
    """Return the versioning the server recorded once a worker claimed the execution."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        described = await handle.describe()
        versioning = described.raw_description.workflow_execution_info.versioning_info
        if versioning.deployment_version.build_id:
            return versioning
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("no worker claimed the execution")
        await asyncio.sleep(0.5)
