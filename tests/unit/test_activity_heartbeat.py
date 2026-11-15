"""Every activity carries a heartbeat timeout and heartbeats while it runs."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from services.orchestration.activity_heartbeat import (
    ACTIVITY_HEARTBEAT_TIMEOUT,
    ActivityHeartbeatInterceptor,
)
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

_observed: dict[str, object] = {}


@activity.defn(name="observe_heartbeats")
async def observe_heartbeats(name: str) -> None:
    _observed[f"{name}:timeout"] = activity.info().heartbeat_timeout
    await asyncio.sleep(0.3)


@workflow.defn
class _HeartbeatWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            observe_heartbeats, "defaulted", start_to_close_timeout=timedelta(minutes=5)
        )
        await workflow.execute_activity(
            observe_heartbeats,
            "explicit",
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(minutes=2),
        )


async def test_activities_get_the_default_heartbeat_timeout_and_heartbeat(monkeypatch):
    beats: list[float] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *details: beats.append(0.0))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"heartbeat-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_HeartbeatWorkflow],
            activities=[observe_heartbeats],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            interceptors=[ActivityHeartbeatInterceptor(interval=timedelta(seconds=0.05))],
        ):
            await env.client.execute_workflow(
                _HeartbeatWorkflow.run, id=f"wf-{uuid.uuid4().hex[:8]}", task_queue=task_queue
            )

    assert _observed["defaulted:timeout"] == ACTIVITY_HEARTBEAT_TIMEOUT
    assert _observed["explicit:timeout"] == timedelta(minutes=2)
    assert len(beats) >= 4
