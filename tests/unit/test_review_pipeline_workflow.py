"""End-to-end pipeline test: PLANNING through ACCEPTANCE_VERIFICATION with real attestations."""

from __future__ import annotations

import uuid

from services.orchestration.temporal_activities import review
from services.orchestration.temporal_activities.bedrock import ModelRequest, ModelResponse
from services.orchestration.temporal_activities.persistence import AuditEvent, ExecutionState
from services.orchestration.temporal_activities.review import (
    review_code,
    review_plan,
    review_security,
    verify_acceptance,
)
from services.orchestration.temporal_workflows.review import (
    CodeReviewWorkflow,
    PlanReviewWorkflow,
    ReviewPipelineInput,
    ReviewPipelineWorkflow,
)
from services.review.attestation import HmacAttestor
from services.review.gate import ReviewGate
from services.review.kernel import FakeReviewKernel
from services.review.types import ModelProvenance, RawReview, Verdict
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


class _InMemorySink:
    def __init__(self) -> None:
        self.records: list = []

    async def persist(self, record) -> str:
        self.records.append(record)
        return record.artifact_id


async def test_pipeline_persists_verified_attestations(monkeypatch):
    raw = RawReview(
        verdict=Verdict.PASS,
        provenance=[ModelProvenance(leg="finder", tier="review", model_id="openai:review")],
        summary="ok",
    )
    sink = _InMemorySink()
    gate = ReviewGate(FakeReviewKernel(raw), sink, HmacAttestor("test-secret"))
    monkeypatch.setattr(review, "build_review_gate", lambda: gate)

    states: list[str] = []
    audits: list[AuditEvent] = []

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        audits.append(event)
        return event.event_id

    @activity.defn(name="invoke_model")
    async def fake_invoke(req: ModelRequest) -> ModelResponse:
        return ModelResponse(
            logical_model=req.logical_model, content="a plan", model_id="openai:planning-model"
        )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"pipeline-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ReviewPipelineWorkflow, PlanReviewWorkflow, CodeReviewWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=[
                fake_persist,
                fake_audit,
                fake_invoke,
                review_plan,
                review_code,
                review_security,
                verify_acceptance,
            ],
        ):
            result = await env.client.execute_workflow(
                ReviewPipelineWorkflow.run,
                ReviewPipelineInput(
                    task_id="T-1", execution_id=f"exec-{uuid.uuid4().hex[:8]}", diff="a diff"
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.completed is True
    assert result.escalated is False
    assert result.blocked is False
    assert result.plan_verdict == "pass"
    assert result.code_verdict == "pass"
    assert result.security_verdict == "pass"
    assert result.acceptance_verdict == "pass"

    assert states == [
        "PLANNING",
        "PLAN_REVIEW",
        "AWAITING_PLAN_APPROVAL",
        "CODE_REVIEW",
        "SECURITY_REVIEW",
        "ACCEPTANCE_VERIFICATION",
        "COMPLETED",
    ]

    persisted = {record.content_hash for record in sink.records}
    stages = {record.metadata["stage"] for record in sink.records}
    assert stages == {
        "plan-review",
        "code-review",
        "security-review",
        "acceptance-verification",
    }
    assert len(persisted) == 4

    for gate_name in ("plan-review", "code-review", "acceptance-verification"):
        assert result.attestations[gate_name] in persisted

    completed = [a for a in audits if a.event_type == "EXECUTION_COMPLETED"]
    reviews = [a for a in audits if a.event_type == "REVIEW_COMPLETED"]
    assert len(completed) == 1
    assert {a.stage for a in reviews} == {
        "PLAN_REVIEW",
        "CODE_REVIEW",
        "SECURITY_REVIEW",
        "ACCEPTANCE_VERIFICATION",
    }
