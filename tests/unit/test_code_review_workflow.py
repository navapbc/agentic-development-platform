"""Workflow-level tests for the code-review and security-review gates (activities mocked)."""

from __future__ import annotations

import uuid

from services.orchestration.temporal_activities.persistence import (
    AuditEvent,
    ExecutionState,
    UsageMetrics,
)
from services.orchestration.temporal_activities.review import ReviewOutcome, ReviewRequest
from services.orchestration.temporal_workflows.review import CodeReviewInput, CodeReviewWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


def _outcome(verdict: str, gate: str, attestation: str) -> ReviewOutcome:
    findings = [] if verdict == "pass" else [f"{gate} finding"]
    return ReviewOutcome(
        verdict=verdict,
        attestation_hash=attestation,
        prompt_version=f"{gate}/v1",
        model_id=f"openai:{gate}",
        summary=verdict,
        blocking_findings=len(findings),
        findings=findings,
    )


async def _run_workflow(code_verdict: str, security_verdict: str):
    states: list[str] = []
    audits: list[AuditEvent] = []
    usages: list[UsageMetrics] = []

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

    @activity.defn(name="review_code")
    async def fake_code(req: ReviewRequest) -> ReviewOutcome:
        return _outcome(code_verdict, "code-review", "code-hash")

    @activity.defn(name="review_security")
    async def fake_security(req: ReviewRequest) -> ReviewOutcome:
        return _outcome(security_verdict, "security-review", "security-hash")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"code-review-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[CodeReviewWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=[fake_persist, fake_audit, fake_usage, fake_code, fake_security],
        ):
            result = await env.client.execute_workflow(
                CodeReviewWorkflow.run,
                CodeReviewInput(
                    task_id="T-1", execution_id=f"exec-{uuid.uuid4().hex[:8]}", diff="a diff"
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, states, audits


async def test_code_review_passes_both_gates():
    result, states, audits = await _run_workflow("pass", "pass")

    assert result.blocked is False
    assert result.code_verdict == "pass"
    assert result.security_verdict == "pass"
    assert result.blocking_findings == []
    assert result.code_attestation == "code-hash"
    assert result.security_attestation == "security-hash"
    assert states == ["CODE_REVIEW", "SECURITY_REVIEW"]
    assert [a.stage for a in audits] == ["CODE_REVIEW", "SECURITY_REVIEW"]


async def test_code_review_blocks_on_code_findings():
    result, _, _ = await _run_workflow("block", "pass")

    assert result.blocked is True
    assert result.blocking_findings == ["code-review finding"]


async def test_code_review_blocks_on_security_findings():
    result, _, _ = await _run_workflow("pass", "block")

    assert result.blocked is True
    assert result.blocking_findings == ["security-review finding"]
