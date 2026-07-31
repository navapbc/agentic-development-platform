"""Tests for the reap workflow stage driving the reap activity."""

from __future__ import annotations

import uuid

from services.orchestration.temporal_activities.reap import (
    ReapActionRecord,
    ReapReport,
    ReapRequest,
)
from services.orchestration.temporal_workflows.reap import ReapInput, ReapWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


async def _run_workflow(*, dry_run: bool):
    calls: list[ReapRequest] = []

    @activity.defn(name="reap_orphans")
    async def fake_reap(req: ReapRequest) -> ReapReport:
        calls.append(req)
        return ReapReport(
            dry_run=req.dry_run,
            scanned=2,
            applied=0 if req.dry_run else 2,
            actions=[
                ReapActionRecord(
                    orphan_id="orphan-1",
                    artifact_type="branch",
                    external_id="agentic-demo-service:agent/x/y",
                    repository="agentic-demo-service",
                    action="delete_branch",
                    applied=not req.dry_run,
                    reason="dry-run" if req.dry_run else "branch deleted",
                )
            ],
        )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"reap-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ReapWorkflow],
            activities=[fake_reap],
        ):
            result = await env.client.execute_workflow(
                ReapWorkflow.run,
                ReapInput(dry_run=dry_run),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, calls


async def test_schedule_default_applies_cleanup():
    result, calls = await _run_workflow(dry_run=False)

    assert calls[0].dry_run is False
    assert result.dry_run is False
    assert result.scanned == 2
    assert result.applied == 2


async def test_dry_run_input_produces_no_applied_actions():
    result, calls = await _run_workflow(dry_run=True)

    assert calls[0].dry_run is True
    assert result.dry_run is True
    assert result.applied == 0
