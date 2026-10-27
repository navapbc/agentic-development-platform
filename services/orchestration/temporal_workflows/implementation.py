"""Temporal workflow for the IMPLEMENTING execution stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

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
        UsageMetrics,
        persist_execution_state,
        record_audit_event,
        record_usage_metrics,
    )
    from services.orchestration.temporal_activities.planning import (
        ContextRequest,
        ContextResult,
        gather_repo_context,
    )
    from services.orchestration.temporal_activities.pull_request import (
        FindOpenPrRequest,
        FindOpenPrResult,
        find_open_pr,
    )

_CAP_BREACH_REASONS = frozenset({TOOL_CALLS_EXCEEDED, WALL_CLOCK_EXCEEDED})
COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
EXISTING_PR_REUSED = "existing_pr_reused"


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
    plan: str = ""
    context_package: dict[str, Any] = field(default_factory=dict)
    owner_workflow_id: str = ""


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
    workspace_path: str = ""
    pr_number: int = 0
    pr_url: str = ""
    escalated: bool = False
    changed_files: list[str] = field(default_factory=list)
    tokens: int = 0
    cache_read_tokens: int = 0
    llm_calls: int = 0
    claim_version: int = 0
    claim_owner_workflow_id: str = ""
    summary: str = ""


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

        claim_version = 0
        claim_owner = ""
        if params.work_unit:
            claim: ClaimResult = await workflow.execute_activity(
                claim_work_unit,
                ClaimRequest(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    work_unit=params.work_unit,
                    agent_id=params.agent_id or params.execution_id,
                    file_impact=list(params.file_impact),
                    owner_workflow_id=params.owner_workflow_id,
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
            claim_version = claim.claim_version
            claim_owner = claim.owner_workflow_id or params.owner_workflow_id or workflow_id

        if params.repo:
            existing: FindOpenPrResult = await workflow.execute_activity(
                find_open_pr,
                FindOpenPrRequest(repo=params.repo, task_id=params.task_id),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            if existing.found:
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:EXISTING_PR_REUSED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="EXISTING_PR_REUSED",
                        stage="IMPLEMENTING",
                        prev_state="IMPLEMENTING",
                        new_state="IMPLEMENTING",
                        decision="reuse",
                        reason=f"existing draft PR #{existing.number} at {existing.url}",
                        workflow_id=workflow_id,
                        artifact_hashes={"pr_url": existing.url},
                    )
                )
                return ImplementationOutput(
                    execution_id=params.execution_id,
                    status=EXISTING_PR_REUSED,
                    terminal_reason=EXISTING_PR_REUSED,
                    claimed=bool(params.work_unit),
                    branch=existing.head,
                    pr_number=existing.number,
                    pr_url=existing.url,
                    claim_owner_workflow_id=claim_owner,
                )

        context_package = params.context_package
        if not context_package and params.repo:
            gathered: ContextResult = await workflow.execute_activity(
                gather_repo_context,
                ContextRequest(
                    repo=params.repo,
                    commit=params.commit,
                    focus=params.focus,
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                ),
                start_to_close_timeout=context_timeout,
                retry_policy=retry,
            )
            context_package = gathered.package

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
                context_package=context_package,
                plan=params.plan,
            ),
            start_to_close_timeout=stage_timeout,
            retry_policy=once,
        )

        await workflow.execute_activity(
            record_usage_metrics,
            UsageMetrics(
                execution_id=params.execution_id,
                task_id=params.task_id,
                stage="IMPLEMENTING",
                workflow_id=workflow_id,
                model_id=result.model_id,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
                total_tokens=result.tokens,
                llm_calls=result.llm_calls,
                tool_calls=result.tool_calls,
                cost_usd=result.cost,
                elapsed_seconds=result.elapsed_seconds,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        llm_calls_exceeded = result.llm_calls > params.max_llm_calls
        tokens_exceeded = max(0, result.tokens - result.cache_read_tokens) > params.max_tokens
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
                workspace_path=result.workspace_path,
                escalated=True,
                changed_files=result.changed_files,
                tokens=result.tokens,
                cache_read_tokens=result.cache_read_tokens,
                llm_calls=result.llm_calls,
                claim_owner_workflow_id=claim_owner,
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
            workspace_path=result.workspace_path,
            escalated=False,
            changed_files=result.changed_files,
            tokens=result.tokens,
            cache_read_tokens=result.cache_read_tokens,
            llm_calls=result.llm_calls,
            claim_version=claim_version,
            claim_owner_workflow_id=claim_owner,
            summary=result.summary,
        )
