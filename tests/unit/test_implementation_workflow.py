"""Tests for the IMPLEMENTING workflow stage."""

from __future__ import annotations

import uuid

from openhands.runtime.caps import COMPLETED, TOOL_CALLS_EXCEEDED
from services.orchestration.temporal_activities.implementation import (
    ClaimRequest,
    ClaimResult,
    ImplementationRequest,
    ImplementationResult,
)
from services.orchestration.temporal_activities.persistence import AuditEvent, ExecutionState
from services.orchestration.temporal_activities.planning import ContextRequest, ContextResult
from services.orchestration.temporal_activities.pull_request import (
    FindOpenPrRequest,
    FindOpenPrResult,
)
from services.orchestration.temporal_workflows.implementation import (
    EXISTING_PR_REUSED,
    ImplementationInput,
    ImplementationWorkflow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


async def _run_workflow(
    *,
    claim_won: bool = True,
    terminal_reason: str = COMPLETED,
    cost: float = 0.0,
    tokens: int = 0,
    llm_calls: int = 0,
    max_cost_usd: float = 5.0,
    max_llm_calls: int = 200,
    max_tokens: int = 2_000_000,
    work_unit: str = "unit-1",
    existing_pr: FindOpenPrResult | None = None,
):
    states: list[str] = []
    audits: list[AuditEvent] = []
    claims: list[ClaimRequest] = []
    stages: list[ImplementationRequest] = []
    contexts: list[ContextRequest] = []
    lookups: list[FindOpenPrRequest] = []

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        audits.append(event)
        return event.event_id

    @activity.defn(name="gather_repo_context")
    async def fake_context(req: ContextRequest) -> ContextResult:
        contexts.append(req)
        return ContextResult(summary="repo context", package={})

    @activity.defn(name="find_open_pr")
    async def fake_find_open_pr(req: FindOpenPrRequest) -> FindOpenPrResult:
        lookups.append(req)
        return existing_pr or FindOpenPrResult(found=False)

    @activity.defn(name="claim_work_unit")
    async def fake_claim(req: ClaimRequest) -> ClaimResult:
        claims.append(req)
        return ClaimResult(claim_id="claim-1", won=claim_won, claim_version=1)

    @activity.defn(name="run_implementation_stage")
    async def fake_stage(req: ImplementationRequest) -> ImplementationResult:
        stages.append(req)
        return ImplementationResult(
            status="finished",
            terminal_reason=terminal_reason,
            tool_calls=3,
            elapsed_seconds=1.5,
            cost=cost,
            tokens=tokens,
            llm_calls=llm_calls,
            branch="agent/T-1/exec-1",
            commit_sha="a" * 40,
            diff_hash="sha256:feed",
            model_id="coding-model",
            prompt_version="implementation/v1",
            workspace_path="/tmp/ws/agent-T-1",
            changed_files=["README.md", "feature.py"],
            summary="done",
        )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"impl-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ImplementationWorkflow],
            activities=[
                fake_persist,
                fake_audit,
                fake_context,
                fake_claim,
                fake_stage,
                fake_find_open_pr,
            ],
        ):
            result = await env.client.execute_workflow(
                ImplementationWorkflow.run,
                ImplementationInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    commit="a" * 40,
                    task="Implement the change",
                    work_unit=work_unit,
                    file_impact=["feature.py"],
                    max_cost_usd=max_cost_usd,
                    max_llm_calls=max_llm_calls,
                    max_tokens=max_tokens,
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, states, audits, claims, stages


def _event_types(audits: list[AuditEvent]) -> list[str]:
    return [a.event_type for a in audits]


async def test_implementation_claims_drives_and_records_audit_events():
    result, states, audits, claims, stages = await _run_workflow()

    assert result.status == "implemented"
    assert result.claimed is True
    assert result.escalated is False
    assert result.diff_hash == "sha256:feed"
    assert result.branch == "agent/T-1/exec-1"
    assert result.workspace_path == "/tmp/ws/agent-T-1"
    assert result.changed_files == ["README.md", "feature.py"]
    assert len(claims) == 1
    assert len(stages) == 1
    assert stages[0].context == "repo context"
    assert states == ["IMPLEMENTING"]
    assert _event_types(audits) == [
        "WORK_UNIT_CLAIMED",
        "IMPLEMENTATION_STARTED",
        "IMPLEMENTATION_COMPLETED",
    ]
    claimed, started, completed = audits
    assert claimed.decision == "won"
    assert started.prompt_version == "implementation/v1"
    assert completed.decision == "implemented"
    assert completed.model_id == "coding-model"
    assert completed.artifact_hashes == {"diff": "sha256:feed"}


async def test_tool_call_cap_breach_fails_bounded():
    result, states, audits, _claims, _stages = await _run_workflow(
        terminal_reason=TOOL_CALLS_EXCEEDED
    )

    assert result.status == "failed"
    assert result.escalated is True
    assert result.terminal_reason == TOOL_CALLS_EXCEEDED
    assert states[-1] == "FAILED"
    failed = audits[-1]
    assert failed.event_type == "IMPLEMENTATION_FAILED"
    assert failed.decision == "cap_breach"
    assert failed.reason == TOOL_CALLS_EXCEEDED
    assert failed.new_state == "FAILED"


async def test_cost_budget_breach_fails_bounded():
    result, states, audits, _claims, _stages = await _run_workflow(cost=9.0, max_cost_usd=5.0)

    assert result.status == "failed"
    assert result.escalated is True
    assert result.terminal_reason == "cost_budget_exceeded"
    assert result.cost == 9.0
    assert states[-1] == "FAILED"
    failed = audits[-1]
    assert failed.event_type == "IMPLEMENTATION_FAILED"
    assert failed.decision == "cap_breach"
    assert failed.reason == "cost_budget_exceeded"


async def test_llm_call_budget_breach_fails_bounded():
    result, states, audits, _claims, _stages = await _run_workflow(llm_calls=201, max_llm_calls=200)

    assert result.status == "failed"
    assert result.escalated is True
    assert result.terminal_reason == "llm_calls_exceeded"
    assert result.llm_calls == 201
    assert states[-1] == "FAILED"
    failed = audits[-1]
    assert failed.decision == "cap_breach"
    assert failed.reason == "llm_calls_exceeded"


async def test_token_budget_breach_fails_bounded():
    result, states, audits, _claims, _stages = await _run_workflow(
        tokens=2_000_001, max_tokens=2_000_000
    )

    assert result.status == "failed"
    assert result.escalated is True
    assert result.terminal_reason == "token_budget_exceeded"
    assert result.tokens == 2_000_001
    assert states[-1] == "FAILED"
    failed = audits[-1]
    assert failed.decision == "cap_breach"
    assert failed.reason == "token_budget_exceeded"


async def test_denied_claim_cancels_without_running():
    result, states, audits, claims, stages = await _run_workflow(claim_won=False)

    assert result.status == "claim_denied"
    assert result.claimed is False
    assert len(claims) == 1
    assert stages == []
    assert states[-1] == "CANCELLED"
    assert _event_types(audits) == ["WORK_UNIT_CLAIMED"]
    assert audits[0].decision == "denied"


async def test_existing_open_pr_short_circuits_without_running_agent():
    result, states, audits, claims, stages = await _run_workflow(
        existing_pr=FindOpenPrResult(
            found=True,
            number=7,
            url="https://github.com/org/agentic-demo-service/pull/7",
            title="ADSER-8 draft",
            head="agent/T-1",
        )
    )

    assert result.status == EXISTING_PR_REUSED
    assert result.terminal_reason == EXISTING_PR_REUSED
    assert result.branch == "agent/T-1"
    assert result.pr_number == 7
    assert result.pr_url == "https://github.com/org/agentic-demo-service/pull/7"
    assert result.claimed is True
    assert len(claims) == 1
    assert stages == []
    assert states == ["IMPLEMENTING"]
    assert _event_types(audits) == ["WORK_UNIT_CLAIMED", "EXISTING_PR_REUSED"]
    reuse = audits[-1]
    assert reuse.decision == "reuse"
    assert reuse.artifact_hashes == {"pr_url": "https://github.com/org/agentic-demo-service/pull/7"}
