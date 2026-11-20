"""The suspendable mixin parks a workflow on a resume signal and continues after it."""

from __future__ import annotations

import uuid

from services.orchestration.suspension import (
    RESUME_SIGNAL,
    ResumeOn,
    SuspendReason,
    Suspension,
)
from services.orchestration.temporal_workflows.suspendable import SuspendableWorkflow
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


@workflow.defn
class _DemoSuspendWorkflow(SuspendableWorkflow):
    @workflow.run
    async def run(self) -> str:
        trigger = await self.await_resume(
            Suspension(
                reason=SuspendReason.AWAITING_FIX,
                resume_on=ResumeOn.NEWER_BUILD,
                stage="demo",
                parked_build_id="1.4.1",
                detail="AttributeError in finalize",
            )
        )
        return f"resumed:{trigger.value if trigger else 'none'}"


@workflow.defn
class _ReviewSuspendWorkflow(SuspendableWorkflow):
    @workflow.run
    async def run(self) -> str:
        trigger = await self.suspend_for(
            SuspendReason.AWAITING_REVIEW,
            stage="review",
            detail="waiting for human approval",
        )
        return f"resumed:{trigger.value if trigger else 'none'}"


async def test_workflow_parks_until_resume_signal_arrives():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"suspend-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_DemoSuspendWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
        ):
            handle = await env.client.start_workflow(
                _DemoSuspendWorkflow.run,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            state = await handle.query(_DemoSuspendWorkflow.suspension_state)
            assert state["reason"] == SuspendReason.AWAITING_FIX.value
            assert state["resume_on"] == ResumeOn.NEWER_BUILD.value
            assert state["parked_build_id"] == "1.4.1"
            assert state["stage"] == "demo"

            await handle.signal(RESUME_SIGNAL, ResumeOn.NEWER_BUILD.value)
            result = await handle.result()

    assert result == "resumed:newer_build"


async def test_suspension_state_clears_after_resume():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"suspend-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_DemoSuspendWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
        ):
            handle = await env.client.start_workflow(
                _DemoSuspendWorkflow.run,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await handle.signal(RESUME_SIGNAL, "")
            result = await handle.result()

    assert result == "resumed:none"


async def test_park_resume_history_replays_deterministically():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"suspend-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_DemoSuspendWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
        ):
            handle = await env.client.start_workflow(
                _DemoSuspendWorkflow.run,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await handle.query(_DemoSuspendWorkflow.suspension_state)
            await handle.signal(RESUME_SIGNAL, ResumeOn.NEWER_BUILD.value)
            await handle.result()
            history = await handle.fetch_history()

    replayer = Replayer(
        workflows=[_DemoSuspendWorkflow],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
        ),
    )

    async def _histories():
        yield history

    results = await replayer.replay_workflows(_histories())
    assert results.replay_failures == {}


async def test_signalled_reason_ignores_mismatched_trigger_and_resumes_on_match():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"review-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_ReviewSuspendWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
        ):
            handle = await env.client.start_workflow(
                _ReviewSuspendWorkflow.run,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            state = await handle.query(_ReviewSuspendWorkflow.suspension_state)
            assert state["reason"] == SuspendReason.AWAITING_REVIEW.value
            assert state["resume_on"] == ResumeOn.HUMAN_APPROVAL.value

            await handle.signal(RESUME_SIGNAL, ResumeOn.CI_RESULT.value)
            still_parked = await handle.query(_ReviewSuspendWorkflow.suspension_state)
            assert still_parked["reason"] == SuspendReason.AWAITING_REVIEW.value

            await handle.signal(RESUME_SIGNAL, ResumeOn.HUMAN_APPROVAL.value)
            result = await handle.result()

    assert result == "resumed:human_approval"
