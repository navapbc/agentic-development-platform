"""Temporal workflow for the IMPLEMENTING execution stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from openhands.runtime.caps import (
        COMPLETED,
        ERROR,
        TOOL_CALLS_EXCEEDED,
        WALL_CLOCK_EXCEEDED,
    )

    from services.orchestration.temporal_activities.implementation import (
        IMPLEMENTATION_PROMPT_VERSION,
        ClaimRequest,
        ClaimResult,
        ImplementationRequest,
        ImplementationResult,
        claim_work_unit,
        run_implementation_stage,
    )
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        ExecutionState,
        persist_execution_state,
        record_audit_event,
    )
    from services.orchestration.temporal_activities.planning import (
        ContextRequest,
        ContextResult,
        gather_repo_context,
    )

_CAP_BREACH_REASONS = frozenset({TOOL_CALLS_EXCEEDED, WALL_CLOCK_EXCEEDED})
COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"


@dataclass
class ImplementationInput:
    task_id: str
    execution_id: str
    repo: str
    commit: str
    task: str
    base: str = "main"
    focus: str = ""
    work_unit: str = ""
    agent_id: str = ""
    file_impact: list[str] = field(default_factory=list)
    max_cost_usd: float = 5.0
    max_llm_calls: int = 200
    max_tokens: int = 2_000_000
    activity_timeout_seconds: float = 2400.0


@dataclass
class ImplementationOutput:
    execution_id: str
    status: str
    terminal_reason: str
    claimed: bool
    cost: float = 0.0
    branch: str = ""
    commit_sha: str = ""
    diff_hash: str = ""
    escalated: bool = False
    changed_files: list[str] = field(default_factory=list)
    tokens: int = 0
    llm_calls: int = 0


@workflow.defn
class ImplementationWorkflow:
    @workflow.run
    async def run(self, params: ImplementationInput) -> ImplementationOutput:
        retry = RetryPolicy(maximum_attempts=3)
        once = RetryPolicy(maximum_attempts=1)
        short = timedelta(seconds=30)
        context_timeout = timedelta(minutes=5)
        stage_timeout = timedelta(seconds=params.activity_timeout_seconds)
        workflow_id = workflow.info().workflow_id

        async def persist(state: str) -> None:
            await workflow.execute_activity(
                persist_execution_state,
                ExecutionState(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    state=state,
                    workflow_id=workflow_id,
                    repository=params.repo or None,
                    repository_sha=params.commit or None,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        async def audit(event: AuditEvent) -> None:
            await workflow.execute_activity(
                record_audit_event,
                event,
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        await persist("IMPLEMENTING")

        if params.work_unit:
            claim: ClaimResult = await workflow.execute_activity(
                claim_work_unit,
                ClaimRequest(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    work_unit=params.work_unit,
                    agent_id=params.agent_id or params.execution_id,
                    file_impact=list(params.file_impact),
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:WORK_UNIT_CLAIMED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="WORK_UNIT_CLAIMED",
                    stage="IMPLEMENTING",
                    new_state="IMPLEMENTING",
                    decision="won" if claim.won else "denied",
                    reason=None if claim.won else f"work unit {params.work_unit} already claimed",
                    workflow_id=workflow_id,
                )
            )
            if not claim.won:
                await persist("CANCELLED")
                return ImplementationOutput(
                    execution_id=params.execution_id,
                    status="claim_denied",
                    terminal_reason="work_unit_claimed",
                    claimed=False,
                )

        context = ""
        if params.repo:
            gathered: ContextResult = await workflow.execute_activity(
                gather_repo_context,
                ContextRequest(repo=params.repo, commit=params.commit, focus=params.focus),
                start_to_close_timeout=context_timeout,
                retry_policy=retry,
            )
            context = gathered.summary

        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:IMPLEMENTATION_STARTED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="IMPLEMENTATION_STARTED",
                stage="IMPLEMENTING",
                prev_state="AWAITING_PLAN_APPROVAL",
                new_state="IMPLEMENTING",
                prompt_version=IMPLEMENTATION_PROMPT_VERSION,
                workflow_id=workflow_id,
            )
        )

        result: ImplementationResult = await workflow.execute_activity(
            run_implementation_stage,
            ImplementationRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                base=params.base,
                commit=params.commit,
                task=params.task,
                context=context,
            ),
            start_to_close_timeout=stage_timeout,
            retry_policy=once,
        )

        llm_calls_exceeded = result.llm_calls > params.max_llm_calls
        tokens_exceeded = result.tokens > params.max_tokens
        cost_exceeded = result.cost > params.max_cost_usd
        budget_breach = llm_calls_exceeded or tokens_exceeded or cost_exceeded
        cap_breach = budget_breach or result.terminal_reason in _CAP_BREACH_REASONS

        if budget_breach or result.terminal_reason != COMPLETED:
            if llm_calls_exceeded:
                reason = LLM_CALLS_EXCEEDED
            elif tokens_exceeded:
                reason = TOKEN_BUDGET_EXCEEDED
            elif cost_exceeded:
                reason = COST_BUDGET_EXCEEDED
            else:
                reason = result.terminal_reason or ERROR
            await persist("FAILED")
            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:IMPLEMENTATION_FAILED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="IMPLEMENTATION_FAILED",
                    stage="IMPLEMENTING",
                    prev_state="IMPLEMENTING",
                    new_state="FAILED",
                    model_id=result.model_id,
                    decision="cap_breach" if cap_breach else "error",
                    reason=reason,
                    prompt_version=result.prompt_version,
                    artifact_hashes={"diff": result.diff_hash},
                    workflow_id=workflow_id,
                )
            )
            return ImplementationOutput(
                execution_id=params.execution_id,
                status="failed",
                terminal_reason=reason,
                claimed=bool(params.work_unit),
                cost=result.cost,
                branch=result.branch,
                commit_sha=result.commit_sha,
                diff_hash=result.diff_hash,
                escalated=True,
                changed_files=result.changed_files,
                tokens=result.tokens,
                llm_calls=result.llm_calls,
            )

        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:IMPLEMENTATION_COMPLETED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="IMPLEMENTATION_COMPLETED",
                stage="IMPLEMENTING",
                prev_state="IMPLEMENTING",
                new_state="IMPLEMENTING",
                model_id=result.model_id,
                decision="implemented",
                prompt_version=result.prompt_version,
                artifact_hashes={"diff": result.diff_hash},
                workflow_id=workflow_id,
            )
        )
        return ImplementationOutput(
            execution_id=params.execution_id,
            status="implemented",
            terminal_reason=result.terminal_reason,
            claimed=bool(params.work_unit),
            cost=result.cost,
            branch=result.branch,
            commit_sha=result.commit_sha,
            diff_hash=result.diff_hash,
            escalated=False,
            changed_files=result.changed_files,
            tokens=result.tokens,
            llm_calls=result.llm_calls,
        )
