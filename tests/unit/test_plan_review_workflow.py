"""Workflow-level tests for the plan-review gate and replan cap (activities mocked)."""

from __future__ import annotations

import uuid

from services.orchestration.temporal_activities.bedrock import ModelRequest, ModelResponse
from services.orchestration.temporal_activities.persistence import (
    AuditEvent,
    ExecutionState,
    UsageMetrics,
)
from services.orchestration.temporal_activities.review import ReviewOutcome, ReviewRequest
from services.orchestration.temporal_workflows.review import PlanReviewInput, PlanReviewWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


async def _run_workflow(verdicts: list[str]):
    states: list[str] = []
    audits: list[AuditEvent] = []
    usages: list[UsageMetrics] = []
    verdict_iter = iter(verdicts)

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        audits.append(event)
        return event.event_id

    @activity.defn(name="record_usage_metrics")
    async def fake_usage(payload: UsageMetrics) -> str:
        usages.append(payload)
        return payload.execution_id

    @activity.defn(name="invoke_model")
    async def fake_invoke(req: ModelRequest) -> ModelResponse:
        return ModelResponse(
            logical_model=req.logical_model,
            content="a change plan",
            model_id="openai:planning-model",
        )

    @activity.defn(name="review_plan")
    async def fake_review(req: ReviewRequest) -> ReviewOutcome:
        verdict = next(verdict_iter)
        findings = [] if verdict == "pass" else ["blocking finding"]
        return ReviewOutcome(
            verdict=verdict,
            attestation_hash="deadbeef",
            prompt_version="plan-review/v1",
            model_id="openai:review-model",
            summary=verdict,
            blocking_findings=len(findings),
            findings=findings,
        )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"plan-review-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PlanReviewWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=[fake_persist, fake_audit, fake_usage, fake_invoke, fake_review],
        ):
            result = await env.client.execute_workflow(
                PlanReviewWorkflow.run,
                PlanReviewInput(task_id="T-1", execution_id=f"exec-{uuid.uuid4().hex[:8]}"),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, states, audits


def _event_types(audits: list[AuditEvent]) -> list[str]:
    return [a.event_type for a in audits]


async def test_plan_review_approves_on_pass():
    result, states, audits = await _run_workflow(["pass"])

    assert result.approved is True
    assert result.escalated is False
    assert result.replans == 0
    assert states == ["PLANNING", "PLAN_REVIEW", "AWAITING_PLAN_APPROVAL"]
    assert _event_types(audits) == ["PLAN_GENERATED", "REVIEW_COMPLETED", "PLAN_APPROVED"]
    approved = audits[-1]
    assert approved.decision == "approve"
    assert approved.artifact_hashes == {"attestation": "deadbeef"}
    assert approved.prompt_version == "plan-review/v1"


async def test_plan_review_revises_within_cap_then_approves():
    result, states, audits = await _run_workflow(["block", "pass"])

    assert result.approved is True
    assert result.escalated is False
    assert result.replans == 1
    assert states == [
        "PLANNING",
        "PLAN_REVIEW",
        "PLAN_REVIEW",
        "AWAITING_PLAN_APPROVAL",
    ]
    assert _event_types(audits) == [
        "PLAN_GENERATED",
        "REVIEW_COMPLETED",
        "PLAN_GENERATED",
        "REVIEW_COMPLETED",
        "PLAN_APPROVED",
    ]


async def test_plan_review_escalates_when_cap_exceeded():
    result, states, audits = await _run_workflow(["block", "block", "block"])

    assert result.approved is False
    assert result.escalated is True
    assert result.replans == 2
    assert states[-1] == "FAILED"
    assert states.count("PLAN_REVIEW") == 3
    escalated = audits[-1]
    assert escalated.event_type == "REVIEW_ESCALATED"
    assert escalated.decision == "escalate"
    assert escalated.new_state == "FAILED"
