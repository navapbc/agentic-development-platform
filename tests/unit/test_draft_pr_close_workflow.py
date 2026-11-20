"""PR-close wait: the draft-PR orchestrator parks after promotion and closes on the signal."""

from __future__ import annotations

import asyncio
import uuid

from services.orchestration.suspension import SuspendReason
from services.orchestration.temporal_activities.persistence import ExecutionState
from services.orchestration.temporal_workflows.draft_pull_request import (
    CANCELLED,
    COMPLETED,
    PR_CLOSED_UNMERGED,
    PR_MERGED,
    DraftPullRequestInput,
    DraftPullRequestWorkflow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from tests.unit.test_draft_pull_request_workflow import (
    _await_approval_park,
    _make_activities,
    _start_ci_worker,
)

SUCCESS = "SUCCESS"


def _pr_close_input() -> DraftPullRequestInput:
    return DraftPullRequestInput(
        task_id="T-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        base="main",
        base_sha="a" * 40,
        task="Implement the change",
        issue_key="T-1",
        file_impact=["feature.py"],
        stored_issue={"summary": "Implement the change"},
        run_reviews=True,
        run_policy=True,
        run_approval=True,
        await_pr_close=True,
        jira_project="AGENTDEV",
        plan_approved=True,
    )


async def _await_pr_close_park(handle) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if state.get("reason") == SuspendReason.AWAITING_PR_CLOSE.value:
            return state
        await asyncio.sleep(0.05)
    return state


async def _run_pr_close(*, merged: bool):
    activities = _make_activities(None, 1, [], [], [], ci_result=SUCCESS)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _pr_close_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_approval_park(handle)
            await handle.signal("submit_pr_approval", args=[True, "release-manager"])

            state = await _await_pr_close_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_PR_CLOSE.value
            assert state["stage"] == "pr_close"

            await handle.signal("submit_pr_closed", merged)
            return await handle.result()


async def test_pr_close_wait_completes_when_merged():
    result = await _run_pr_close(merged=True)

    assert result.status == PR_MERGED
    assert result.pr_merged is True
    assert result.pr_close_status == COMPLETED
    assert result.promoted is True


async def test_await_pr_close_path_persists_awaiting_pr_close():
    states: list[str] = []

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    activities = _make_activities(None, 1, [], [], [], ci_result=SUCCESS)
    activities = [
        activity_fn
        for activity_fn in activities
        if activity_fn.__temporal_activity_definition.name != "persist_execution_state"
    ]
    activities.insert(0, fake_persist)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _pr_close_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_approval_park(handle)
            await handle.signal("submit_pr_approval", args=[True, "release-manager"])
            state = await _await_pr_close_park(handle)
            await handle.signal("submit_pr_closed", True)
            result = await handle.result()

    assert state["reason"] == SuspendReason.AWAITING_PR_CLOSE.value
    assert result.pr_close_status == COMPLETED
    assert "AWAITING_PR_CLOSE" in states
    assert states[-1] == COMPLETED


async def test_pr_close_wait_cancels_when_closed_unmerged():
    result = await _run_pr_close(merged=False)

    assert result.status == PR_CLOSED_UNMERGED
    assert result.pr_merged is False
    assert result.pr_close_status == CANCELLED
    assert result.promoted is True
