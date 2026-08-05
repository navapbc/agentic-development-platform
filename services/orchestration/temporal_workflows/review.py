"""Review workflows: plan-review with replan cap, and sequential code and security review."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.bedrock import (
        ModelRequest,
        ModelResponse,
        invoke_model,
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
    from services.orchestration.temporal_activities.review import (
        ReviewOutcome,
        ReviewRequest,
        review_code,
        review_plan,
        review_security,
        verify_acceptance,
    )

REPLAN_CAP = 2
PLANNING_MODEL = "planning-model"


@dataclass
class PlanReviewInput:
    task_id: str
    execution_id: str
    repo: str = ""
    commit: str = ""
    focus: str = ""
    plan: str = ""


@dataclass
class PlanReviewOutput:
    execution_id: str
    verdict: str
    approved: bool
    escalated: bool
    replans: int
    attestation_hash: str = ""


def _plan_prompt(params: PlanReviewInput, context_summary: str, prior_findings: list[str]) -> str:
    parts = [f"Task {params.task_id}: produce a concrete change plan."]
    if context_summary:
        parts.append(f"Repository context:\n{context_summary}")
    if prior_findings:
        joined = "\n".join(f"- {f}" for f in prior_findings)
        parts.append(f"Revise the plan to resolve these blocking review findings:\n{joined}")
    return "\n\n".join(parts)


@workflow.defn
class PlanReviewWorkflow:
    @workflow.run
    async def run(self, params: PlanReviewInput) -> PlanReviewOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        model_timeout = timedelta(minutes=5)
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

        async def usage(stage: str, outcome: ReviewOutcome) -> None:
            await workflow.execute_activity(
                record_usage_metrics,
                UsageMetrics(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    stage=stage,
                    workflow_id=workflow_id,
                    model_id=outcome.model_id,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                    cache_read_tokens=outcome.cache_read_tokens,
                    cache_write_tokens=outcome.cache_write_tokens,
                    total_tokens=outcome.total_tokens,
                    llm_calls=outcome.llm_calls,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        await persist("PLANNING")

        context_summary = ""
        if params.repo:
            context: ContextResult = await workflow.execute_activity(
                gather_repo_context,
                ContextRequest(repo=params.repo, commit=params.commit, focus=params.focus),
                start_to_close_timeout=model_timeout,
                retry_policy=retry,
            )
            context_summary = context.summary

        draft: ModelResponse = await workflow.execute_activity(
            invoke_model,
            ModelRequest(
                logical_model=PLANNING_MODEL,
                prompt=_plan_prompt(params, context_summary, []),
                max_tokens=2048,
            ),
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )
        plan = draft.content or params.plan
        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:PLAN_GENERATED:0",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="PLAN_GENERATED",
                stage="PLANNING",
                new_state="PLANNING",
                model_id=draft.model_id,
                workflow_id=workflow_id,
            )
        )

        replans = 0
        attempt = 0
        plan_prompt = 0
        plan_completion = 0
        plan_cache_read = 0
        plan_cache_write = 0
        plan_total = 0
        plan_calls = 0
        while True:
            await persist("PLAN_REVIEW")
            outcome: ReviewOutcome = await workflow.execute_activity(
                review_plan,
                ReviewRequest(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    subject=plan,
                    context_ref=params.commit,
                    context_summary=context_summary,
                ),
                start_to_close_timeout=model_timeout,
                retry_policy=retry,
            )
            plan_prompt += outcome.prompt_tokens
            plan_completion += outcome.completion_tokens
            plan_cache_read += outcome.cache_read_tokens
            plan_cache_write += outcome.cache_write_tokens
            plan_total += outcome.total_tokens
            plan_calls += outcome.llm_calls
            await usage(
                "PLAN_REVIEW",
                replace(
                    outcome,
                    prompt_tokens=plan_prompt,
                    completion_tokens=plan_completion,
                    cache_read_tokens=plan_cache_read,
                    cache_write_tokens=plan_cache_write,
                    total_tokens=plan_total,
                    llm_calls=plan_calls,
                ),
            )
            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:REVIEW_COMPLETED:{attempt}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="REVIEW_COMPLETED",
                    stage="PLAN_REVIEW",
                    prev_state="PLAN_REVIEW",
                    new_state="PLAN_REVIEW",
                    model_id=outcome.model_id,
                    decision=outcome.verdict,
                    prompt_version=outcome.prompt_version,
                    artifact_hashes={"attestation": outcome.attestation_hash},
                    workflow_id=workflow_id,
                )
            )

            if outcome.verdict == "pass":
                await persist("AWAITING_PLAN_APPROVAL")
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:PLAN_APPROVED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="PLAN_APPROVED",
                        stage="PLAN_REVIEW",
                        prev_state="PLAN_REVIEW",
                        new_state="AWAITING_PLAN_APPROVAL",
                        decision="approve",
                        prompt_version=outcome.prompt_version,
                        artifact_hashes={"attestation": outcome.attestation_hash},
                        workflow_id=workflow_id,
                    )
                )
                return PlanReviewOutput(
                    execution_id=params.execution_id,
                    verdict=outcome.verdict,
                    approved=True,
                    escalated=False,
                    replans=replans,
                    attestation_hash=outcome.attestation_hash,
                )

            if replans >= REPLAN_CAP:
                await persist("FAILED")
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:REVIEW_ESCALATED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="REVIEW_ESCALATED",
                        stage="PLAN_REVIEW",
                        prev_state="PLAN_REVIEW",
                        new_state="FAILED",
                        decision="escalate",
                        reason=f"plan blocked after {replans} replans (cap {REPLAN_CAP})",
                        prompt_version=outcome.prompt_version,
                        artifact_hashes={"attestation": outcome.attestation_hash},
                        workflow_id=workflow_id,
                    )
                )
                return PlanReviewOutput(
                    execution_id=params.execution_id,
                    verdict=outcome.verdict,
                    approved=False,
                    escalated=True,
                    replans=replans,
                    attestation_hash=outcome.attestation_hash,
                )

            replans += 1
            attempt += 1
            revised: ModelResponse = await workflow.execute_activity(
                invoke_model,
                ModelRequest(
                    logical_model=PLANNING_MODEL,
                    prompt=_plan_prompt(params, context_summary, outcome.findings),
                    max_tokens=2048,
                ),
                start_to_close_timeout=model_timeout,
                retry_policy=retry,
            )
            plan = revised.content or plan
            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:PLAN_GENERATED:{attempt}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="PLAN_GENERATED",
                    stage="PLANNING",
                    prev_state="PLAN_REVIEW",
                    new_state="PLANNING",
                    model_id=revised.model_id,
                    reason=f"replan {replans}",
                    workflow_id=workflow_id,
                )
            )


@dataclass
class CodeReviewInput:
    task_id: str
    execution_id: str
    diff: str
    repo: str = ""
    commit: str = ""
    focus: str = ""


@dataclass
class CodeReviewOutput:
    execution_id: str
    code_verdict: str
    security_verdict: str
    blocked: bool
    blocking_findings: list[str] = field(default_factory=list)
    code_attestation: str = ""
    security_attestation: str = ""


@workflow.defn
class CodeReviewWorkflow:
    @workflow.run
    async def run(self, params: CodeReviewInput) -> CodeReviewOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        model_timeout = timedelta(minutes=5)
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

        async def usage(stage: str, outcome: ReviewOutcome) -> None:
            await workflow.execute_activity(
                record_usage_metrics,
                UsageMetrics(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    stage=stage,
                    workflow_id=workflow_id,
                    model_id=outcome.model_id,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                    cache_read_tokens=outcome.cache_read_tokens,
                    cache_write_tokens=outcome.cache_write_tokens,
                    total_tokens=outcome.total_tokens,
                    llm_calls=outcome.llm_calls,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        context_summary = ""
        if params.repo:
            context: ContextResult = await workflow.execute_activity(
                gather_repo_context,
                ContextRequest(repo=params.repo, commit=params.commit, focus=params.focus),
                start_to_close_timeout=model_timeout,
                retry_policy=retry,
            )
            context_summary = context.summary

        request = ReviewRequest(
            task_id=params.task_id,
            execution_id=params.execution_id,
            subject=params.diff,
            context_ref=params.commit,
            context_summary=context_summary,
        )

        await persist("CODE_REVIEW")
        code: ReviewOutcome = await workflow.execute_activity(
            review_code,
            request,
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )
        await usage("CODE_REVIEW", code)
        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:REVIEW_COMPLETED:CODE_REVIEW",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="REVIEW_COMPLETED",
                stage="CODE_REVIEW",
                prev_state="CODE_REVIEW",
                new_state="CODE_REVIEW",
                model_id=code.model_id,
                decision=code.verdict,
                prompt_version=code.prompt_version,
                artifact_hashes={"attestation": code.attestation_hash},
                workflow_id=workflow_id,
            )
        )

        await persist("SECURITY_REVIEW")
        security: ReviewOutcome = await workflow.execute_activity(
            review_security,
            request,
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )
        await usage("SECURITY_REVIEW", security)
        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:REVIEW_COMPLETED:SECURITY_REVIEW",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="REVIEW_COMPLETED",
                stage="SECURITY_REVIEW",
                prev_state="SECURITY_REVIEW",
                new_state="SECURITY_REVIEW",
                model_id=security.model_id,
                decision=security.verdict,
                prompt_version=security.prompt_version,
                artifact_hashes={"attestation": security.attestation_hash},
                workflow_id=workflow_id,
            )
        )

        blocking_findings: list[str] = []
        if code.verdict == "block":
            blocking_findings.extend(code.findings)
        if security.verdict == "block":
            blocking_findings.extend(security.findings)

        return CodeReviewOutput(
            execution_id=params.execution_id,
            code_verdict=code.verdict,
            security_verdict=security.verdict,
            blocked=bool(blocking_findings),
            blocking_findings=blocking_findings,
            code_attestation=code.attestation_hash,
            security_attestation=security.attestation_hash,
        )


@dataclass
class ReviewPipelineInput:
    task_id: str
    execution_id: str
    diff: str
    repo: str = ""
    commit: str = ""
    focus: str = ""
    acceptance_evidence: str = ""


@dataclass
class ReviewPipelineOutput:
    execution_id: str
    plan_verdict: str
    code_verdict: str
    security_verdict: str
    acceptance_verdict: str
    completed: bool
    escalated: bool
    blocked: bool
    attestations: dict[str, str] = field(default_factory=dict)


@workflow.defn
class ReviewPipelineWorkflow:
    @workflow.run
    async def run(self, params: ReviewPipelineInput) -> ReviewPipelineOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        model_timeout = timedelta(minutes=5)
        workflow_id = workflow.info().workflow_id
        attestations: dict[str, str] = {}

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

        async def usage(stage: str, outcome: ReviewOutcome) -> None:
            await workflow.execute_activity(
                record_usage_metrics,
                UsageMetrics(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    stage=stage,
                    workflow_id=workflow_id,
                    model_id=outcome.model_id,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                    cache_read_tokens=outcome.cache_read_tokens,
                    cache_write_tokens=outcome.cache_write_tokens,
                    total_tokens=outcome.total_tokens,
                    llm_calls=outcome.llm_calls,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        plan: PlanReviewOutput = await workflow.execute_child_workflow(
            PlanReviewWorkflow.run,
            PlanReviewInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                commit=params.commit,
                focus=params.focus,
            ),
            id=f"{workflow_id}-plan",
        )
        attestations["plan-review"] = plan.attestation_hash
        if not plan.approved:
            return ReviewPipelineOutput(
                execution_id=params.execution_id,
                plan_verdict=plan.verdict,
                code_verdict="",
                security_verdict="",
                acceptance_verdict="",
                completed=False,
                escalated=plan.escalated,
                blocked=False,
                attestations=attestations,
            )

        code: CodeReviewOutput = await workflow.execute_child_workflow(
            CodeReviewWorkflow.run,
            CodeReviewInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                diff=params.diff,
                repo=params.repo,
                commit=params.commit,
                focus=params.focus,
            ),
            id=f"{workflow_id}-code",
        )
        attestations["code-review"] = code.code_attestation
        attestations["security-review"] = code.security_attestation
        if code.blocked:
            return ReviewPipelineOutput(
                execution_id=params.execution_id,
                plan_verdict=plan.verdict,
                code_verdict=code.code_verdict,
                security_verdict=code.security_verdict,
                acceptance_verdict="",
                completed=False,
                escalated=False,
                blocked=True,
                attestations=attestations,
            )

        await persist("ACCEPTANCE_VERIFICATION")
        acceptance: ReviewOutcome = await workflow.execute_activity(
            verify_acceptance,
            ReviewRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                subject=params.acceptance_evidence or params.diff,
                context_ref=params.commit,
            ),
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )
        attestations["acceptance-verification"] = acceptance.attestation_hash
        await usage("ACCEPTANCE_VERIFICATION", acceptance)
        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:REVIEW_COMPLETED:ACCEPTANCE_VERIFICATION",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="REVIEW_COMPLETED",
                stage="ACCEPTANCE_VERIFICATION",
                prev_state="ACCEPTANCE_VERIFICATION",
                new_state="ACCEPTANCE_VERIFICATION",
                model_id=acceptance.model_id,
                decision=acceptance.verdict,
                prompt_version=acceptance.prompt_version,
                artifact_hashes={"attestation": acceptance.attestation_hash},
                workflow_id=workflow_id,
            )
        )

        accepted = acceptance.verdict == "pass"
        await persist("COMPLETED" if accepted else "FAILED")
        if accepted:
            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:EXECUTION_COMPLETED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="EXECUTION_COMPLETED",
                    stage="ACCEPTANCE_VERIFICATION",
                    prev_state="ACCEPTANCE_VERIFICATION",
                    new_state="COMPLETED",
                    workflow_id=workflow_id,
                )
            )

        return ReviewPipelineOutput(
            execution_id=params.execution_id,
            plan_verdict=plan.verdict,
            code_verdict=code.code_verdict,
            security_verdict=code.security_verdict,
            acceptance_verdict=acceptance.verdict,
            completed=accepted,
            escalated=False,
            blocked=not accepted,
            attestations=attestations,
        )
