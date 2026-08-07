"""Temporal orchestrator that drives a bounded task from claim to draft PR and Jira linkback."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.temporal_activities.ci import (
        PollCIRequest,
        PollCIResult,
        ReadCILogRequest,
        ReadCILogResult,
        TriggerCIRequest,
        TriggerCIResult,
        poll_ci,
        read_ci_log,
        trigger_ci,
    )
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        ExecutionState,
        persist_execution_state,
        record_audit_event,
    )
    from services.orchestration.temporal_activities.policy import (
        EvaluatePromotionRequest,
        EvaluatePromotionResult,
        evaluate_promotion,
    )
    from services.orchestration.temporal_activities.pull_request import (
        CommitPushRequest,
        CommitPushResult,
        LinkbackRequest,
        LinkbackResult,
        ParkTaskRequest,
        ParkTaskResult,
        UnparkTaskRequest,
        commit_and_push,
        link_pr_to_issue,
        mark_task_parked,
        unmark_task_parked,
    )
    from services.orchestration.temporal_activities.repair import (
        AnalyzeCIFailureRequest,
        AnalyzeCIFailureResult,
        ApplyCIRepairRequest,
        ApplyCIRepairResult,
        analyze_ci_failure,
        apply_ci_repair,
    )
    from services.orchestration.temporal_activities.review import (
        ReviewOutcome,
        ReviewRequest,
        review_code,
        review_security,
        verify_acceptance,
    )
    from services.orchestration.temporal_activities.security import (
        ScanChangeRequest,
        ScanChangeResult,
        scan_change,
    )
    from services.orchestration.temporal_activities.suspension import (
        ClearSuspensionRequest,
        RecordSuspensionRequest,
        clear_suspension,
        record_suspension,
    )
    from services.orchestration.temporal_workflows.branch_sync import (
        BranchSyncInput,
        BranchSyncOutput,
        BranchSyncWorkflow,
    )
    from services.orchestration.temporal_workflows.implementation import (
        EXISTING_PR_REUSED,
        ImplementationInput,
        ImplementationOutput,
        ImplementationWorkflow,
    )
    from services.orchestration.temporal_workflows.pull_request import (
        PullRequestInput,
        PullRequestOutput,
        PullRequestWorkflow,
    )

from adapters.interfaces.ci_host import SUCCESS

from services.orchestration.suspension import ResumeOn, SuspendReason, Suspension
from services.orchestration.temporal_workflows.suspendable import (
    SuspendableWorkflow,
    is_code_defect,
)

CLAIM_DENIED = "claim_denied"
IMPL_FAILED = "impl_failed"
SYNC_PAUSED = "sync_paused"
DRAFT_PR_OPEN = "draft_pr_open"
CI_PASSED = "ci_passed"
CI_FAILED = "ci_failed"
CI_RUNNING = "CI_RUNNING"
REPAIRING = "REPAIRING"
REVIEW_PASSED = "review_passed"
REVIEW_BLOCKED = "review_blocked"
POLICY_ALLOWED = "policy_allowed"
POLICY_DENIED = "policy_denied"
POLICY_EVALUATION = "POLICY_EVALUATION"

_SEQUENCE_STRIDE = 10
_COMMIT_OFFSET = 2
_OPEN_PR_OFFSET = 3
_CI_TRIGGER_OFFSET = 4
_REPAIR_STRIDE = 2

_CODE_REVIEW = "CODE_REVIEW"
_SECURITY_REVIEW = "SECURITY_REVIEW"
_ACCEPTANCE_VERIFICATION = "ACCEPTANCE_VERIFICATION"


@dataclass
class DraftPullRequestInput:
    task_id: str
    execution_id: str
    repo: str
    base: str
    base_sha: str
    task: str
    issue_key: str = ""
    focus: str = ""
    work_unit: str = "unit-1"
    agent_id: str = "agent-a"
    file_impact: list[str] = field(default_factory=list)
    commit_message: str = "Apply bounded agent change"
    title: str = ""
    body: str = ""
    stored_issue: dict[str, Any] = field(default_factory=dict)
    max_cost_usd: float = 5.0
    max_llm_calls: int = 200
    max_tokens: int = 2_000_000
    activity_timeout_seconds: float = 2400.0
    build_id: str = ""
    parked_status: str = ""
    ci_job: str = ""
    ci_poll_interval_seconds: float = 60.0
    ci_poll_max_attempts: int = 20
    max_repair_attempts: int = 2
    run_reviews: bool = False
    run_policy: bool = False
    jira_project: str = ""
    plan_approved: bool = False
    jira_snapshot_current: bool = True


@dataclass
class DraftPullRequestOutput:
    execution_id: str
    status: str
    terminal_reason: str = ""
    branch: str = ""
    pr_number: int = 0
    pr_url: str = ""
    linkback: str = "none"
    reused: bool = False
    ci_status: str = ""
    ci_build_id: str = ""
    ci_build_number: int = 0
    repair_attempts: int = 0
    review_status: str = ""
    code_verdict: str = ""
    security_verdict: str = ""
    acceptance_verdict: str = ""
    review_attestations: dict[str, str] = field(default_factory=dict)
    policy_status: str = ""
    policy_bundle_version: str = ""
    policy_next_action: str = ""
    policy_reasons: list[str] = field(default_factory=list)
    scan_blocking_findings: int = 0


def _project_from_issue(issue_key: str) -> str:
    return issue_key.split("-", 1)[0] if "-" in issue_key else ""


def _change_subject(impl: ImplementationOutput, commit_sha: str) -> str:
    files = "\n".join(impl.changed_files) if impl.changed_files else "(no file list)"
    return f"Change on branch {impl.branch} at {commit_sha}.\nChanged files:\n{files}"


@workflow.defn
class DraftPullRequestWorkflow(SuspendableWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._sync: BranchSyncOutput | None = None
        self._commit: CommitPushResult | None = None
        self._pr: PullRequestOutput | None = None

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        workflow_id = workflow.info().workflow_id

        title = params.title or f"[{params.task_id}] {params.task}"

        async def linkback_existing(pr_url: str, branch: str) -> str:
            if not params.issue_key:
                return "none"
            result: LinkbackResult = await workflow.execute_activity(
                link_pr_to_issue,
                LinkbackRequest(
                    issue_key=params.issue_key,
                    execution_id=params.execution_id,
                    pr_url=pr_url,
                    pr_title=title,
                    repo=params.repo,
                    branch=branch,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            return result.action

        impl: ImplementationOutput = await workflow.execute_child_workflow(
            ImplementationWorkflow.run,
            ImplementationInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                commit=params.base_sha,
                task=params.task,
                base=params.base,
                focus=params.focus,
                work_unit=params.work_unit,
                agent_id=params.agent_id,
                file_impact=list(params.file_impact),
                max_cost_usd=params.max_cost_usd,
                max_llm_calls=params.max_llm_calls,
                max_tokens=params.max_tokens,
                activity_timeout_seconds=params.activity_timeout_seconds,
            ),
            id=f"{workflow_id}-impl",
        )

        if impl.status == EXISTING_PR_REUSED:
            action = await linkback_existing(impl.pr_url, impl.branch)
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=EXISTING_PR_REUSED,
                terminal_reason=EXISTING_PR_REUSED,
                branch=impl.branch,
                pr_number=impl.pr_number,
                pr_url=impl.pr_url,
                linkback=action,
                reused=True,
            )

        if impl.status == "claim_denied":
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=CLAIM_DENIED,
                terminal_reason=impl.terminal_reason,
            )

        if impl.status != "implemented":
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=IMPL_FAILED,
                terminal_reason=impl.terminal_reason,
                branch=impl.branch,
            )

        while True:
            try:
                outcome = await self._finalize(params, impl, workflow_id, title)
                break
            except Exception as exc:  # noqa: BLE001
                if not is_code_defect(exc):
                    raise
                suspension = Suspension(
                    reason=SuspendReason.AWAITING_FIX,
                    resume_on=ResumeOn.NEWER_BUILD,
                    stage="finalize",
                    parked_build_id=params.build_id,
                    detail=str(exc),
                )
                await workflow.execute_activity(
                    record_suspension,
                    RecordSuspensionRequest(
                        workflow_id=workflow_id,
                        reason=suspension.reason.value,
                        resume_on=suspension.resume_on.value,
                        parked_build_id=suspension.parked_build_id,
                        stage=suspension.stage,
                        detail=suspension.detail,
                    ),
                    start_to_close_timeout=short,
                    retry_policy=retry,
                )
                parked_from = ""
                if params.issue_key and params.parked_status:
                    park: ParkTaskResult = await workflow.execute_activity(
                        mark_task_parked,
                        ParkTaskRequest(
                            issue_key=params.issue_key,
                            parked_status=params.parked_status,
                        ),
                        start_to_close_timeout=short,
                        retry_policy=retry,
                    )
                    parked_from = park.previous_status
                await self.await_resume(suspension)
                if params.issue_key and parked_from:
                    await workflow.execute_activity(
                        unmark_task_parked,
                        UnparkTaskRequest(
                            issue_key=params.issue_key,
                            target_status=parked_from,
                        ),
                        start_to_close_timeout=short,
                        retry_policy=retry,
                    )
                await workflow.execute_activity(
                    clear_suspension,
                    ClearSuspensionRequest(workflow_id=workflow_id),
                    start_to_close_timeout=short,
                    retry_policy=retry,
                )

        if params.ci_job and outcome.status == DRAFT_PR_OPEN:
            outcome = await self._ci_and_repair(params, impl, outcome, workflow_id)
            if params.run_reviews and outcome.status == CI_PASSED:
                outcome = await self._review(params, impl, outcome, workflow_id)
                if params.run_policy and outcome.status == REVIEW_PASSED:
                    return await self._policy(params, impl, outcome, workflow_id)
        return outcome

    async def _finalize(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        workflow_id: str,
        title: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        commit_timeout = timedelta(minutes=5)
        sequence_base = impl.claim_version * _SEQUENCE_STRIDE

        if self._sync is None:
            self._sync = await workflow.execute_child_workflow(
                BranchSyncWorkflow.run,
                BranchSyncInput(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    repo=params.repo,
                    base=params.base,
                    base_sha=params.base_sha,
                    branch=impl.branch,
                    workspace_path=impl.workspace_path,
                    issue_key=params.issue_key,
                    stored_issue=dict(params.stored_issue),
                ),
                id=f"{workflow_id}-sync",
            )
        sync = self._sync
        if sync.status != "synced":
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=SYNC_PAUSED,
                terminal_reason=sync.terminal_reason,
                branch=impl.branch,
            )

        if self._commit is None:
            self._commit = await workflow.execute_activity(
                commit_and_push,
                CommitPushRequest(
                    repo=params.repo,
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    base=params.base,
                    commit=params.base_sha,
                    message=params.commit_message,
                    sequence=sequence_base + _COMMIT_OFFSET,
                ),
                start_to_close_timeout=commit_timeout,
                retry_policy=retry,
            )
        commit = self._commit

        body = params.body or (
            f"Automated draft for {params.issue_key} (execution {params.execution_id})."
        )
        if self._pr is None:
            self._pr = await workflow.execute_child_workflow(
                PullRequestWorkflow.run,
                PullRequestInput(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    repo=params.repo,
                    base=params.base,
                    branch=commit.branch,
                    title=title,
                    body=body,
                    issue_key=params.issue_key,
                    base_sha=params.base_sha,
                    sequence=sequence_base + _OPEN_PR_OFFSET,
                ),
                id=f"{workflow_id}-pr",
            )
        pr = self._pr

        return DraftPullRequestOutput(
            execution_id=params.execution_id,
            status=DRAFT_PR_OPEN,
            terminal_reason=impl.terminal_reason,
            branch=commit.branch,
            pr_number=pr.number,
            pr_url=pr.url,
            linkback=pr.linkback,
        )

    async def _ci_and_repair(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        result = await self._run_ci(params, impl, outcome, workflow_id, attempt=0)
        repairs = 0
        while result.status == CI_FAILED and repairs < params.max_repair_attempts:
            attempt = repairs + 1
            applied = await self._repair(params, impl, result, workflow_id, attempt)
            if not applied:
                break
            repairs = attempt
            result = await self._run_ci(params, impl, result, workflow_id, attempt=repairs)

        if result.status == CI_FAILED:
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:REPAIR_EXHAUSTED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="REPAIR_EXHAUSTED",
                    stage="repair",
                    new_state=REPAIRING,
                    workflow_id=workflow_id,
                    reason=str(repairs),
                )
            )
        return replace(result, repair_attempts=repairs)

    async def _repair(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
        attempt: int,
    ) -> bool:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        commit_timeout = timedelta(minutes=5)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=REPAIRING,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:REPAIR_STARTED:{attempt}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="REPAIR_STARTED",
                stage="repair",
                new_state=REPAIRING,
                workflow_id=workflow_id,
                reason=str(attempt),
                decision=outcome.ci_build_id,
            )
        )

        log: ReadCILogResult = await workflow.execute_activity(
            read_ci_log,
            ReadCILogRequest(
                repo=params.repo,
                job=params.ci_job,
                build_number=outcome.ci_build_number,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        plan: AnalyzeCIFailureResult = await workflow.execute_activity(
            analyze_ci_failure,
            AnalyzeCIFailureRequest(failure_summary=log.failure_summary),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        if not plan.actionable:
            return False

        repair: ApplyCIRepairResult = await workflow.execute_activity(
            apply_ci_repair,
            ApplyCIRepairRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                base=params.base,
                commit=commit_sha,
                instruction=plan.instruction,
                failure_summary=log.failure_summary,
            ),
            start_to_close_timeout=timedelta(seconds=params.activity_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if not repair.applied:
            return False

        sequence = (
            impl.claim_version * _SEQUENCE_STRIDE + _OPEN_PR_OFFSET + _REPAIR_STRIDE * attempt
        )
        self._commit = await workflow.execute_activity(
            commit_and_push,
            CommitPushRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                base=params.base,
                commit=params.base_sha,
                message=params.commit_message,
                sequence=sequence,
            ),
            start_to_close_timeout=commit_timeout,
            retry_policy=retry,
        )
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:REPAIR_APPLIED:{attempt}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="REPAIR_APPLIED",
                stage="repair",
                new_state=REPAIRING,
                workflow_id=workflow_id,
                reason=str(attempt),
                artifact_hashes={"files": ",".join(repair.changed_files)},
            )
        )
        return True

    async def _run_ci(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
        attempt: int = 0,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        sequence = (
            impl.claim_version * _SEQUENCE_STRIDE + _CI_TRIGGER_OFFSET + _REPAIR_STRIDE * attempt
        )
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=CI_RUNNING,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        trigger: TriggerCIResult = await workflow.execute_activity(
            trigger_ci,
            TriggerCIRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                job=params.ci_job,
                branch=outcome.branch,
                commit_sha=commit_sha,
                sequence=sequence,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        build_id = str(trigger.build_number)

        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:CI_TRIGGERED:{build_id}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="CI_TRIGGERED",
                stage="ci",
                new_state=CI_RUNNING,
                workflow_id=workflow_id,
                reason=build_id,
                artifact_hashes={"build_url": trigger.url},
            )
        )

        result = await self._await_ci_result(params, trigger, build_id)
        passed = result.result == SUCCESS
        event_type = "CI_PASSED" if passed else "CI_FAILED"

        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:{event_type}:{build_id}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type=event_type,
                stage="ci",
                new_state=CI_RUNNING,
                workflow_id=workflow_id,
                reason=build_id,
                decision=result.result,
                artifact_hashes={"build_url": result.url},
            )
        )

        return replace(
            outcome,
            status=CI_PASSED if passed else CI_FAILED,
            ci_status="passed" if passed else "failed",
            ci_build_id=build_id,
            ci_build_number=trigger.build_number,
        )

    async def _await_ci_result(
        self,
        params: DraftPullRequestInput,
        trigger: TriggerCIResult,
        build_id: str,
    ) -> PollCIResult:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        request = PollCIRequest(
            repo=params.repo,
            job=params.ci_job,
            build_number=trigger.build_number,
        )
        suspension = Suspension(
            reason=SuspendReason.AWAITING_CI,
            resume_on=ResumeOn.CI_RESULT,
            stage="ci",
            parked_build_id=build_id,
        )
        self._suspension = suspension
        poll_interval = (
            timedelta(seconds=params.ci_poll_interval_seconds)
            if params.ci_poll_interval_seconds > 0
            else None
        )

        result: PollCIResult | None = None
        for _ in range(max(1, params.ci_poll_max_attempts)):
            poll: PollCIResult = await workflow.execute_activity(
                poll_ci,
                request,
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            if poll.completed:
                result = poll
                break
            await self.wait_resume_or_timeout(suspension, timeout=poll_interval)

        self._suspension = None
        if result is None:
            result = await workflow.execute_activity(
                poll_ci,
                request,
                start_to_close_timeout=short,
                retry_policy=retry,
            )
        return result

    async def _review(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha
        subject = _change_subject(impl, commit_sha)
        attestations: dict[str, str] = {}

        code = await self._review_gate(
            params, review_code, _CODE_REVIEW, subject, commit_sha, impl, workflow_id
        )
        attestations["code-review"] = code.attestation_hash
        security = await self._review_gate(
            params, review_security, _SECURITY_REVIEW, subject, commit_sha, impl, workflow_id
        )
        attestations["security-review"] = security.attestation_hash

        if code.verdict == "block" or security.verdict == "block":
            return replace(
                outcome,
                status=REVIEW_BLOCKED,
                review_status="blocked",
                code_verdict=code.verdict,
                security_verdict=security.verdict,
                review_attestations=attestations,
            )

        acceptance = await self._review_gate(
            params,
            verify_acceptance,
            _ACCEPTANCE_VERIFICATION,
            subject,
            commit_sha,
            impl,
            workflow_id,
        )
        attestations["acceptance-verification"] = acceptance.attestation_hash
        blocked = acceptance.verdict == "block"

        return replace(
            outcome,
            status=REVIEW_BLOCKED if blocked else REVIEW_PASSED,
            review_status="blocked" if blocked else "passed",
            code_verdict=code.verdict,
            security_verdict=security.verdict,
            acceptance_verdict=acceptance.verdict,
            review_attestations=attestations,
        )

    async def _policy(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        scan_timeout = timedelta(minutes=10)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=POLICY_EVALUATION,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        scan: ScanChangeResult = await workflow.execute_activity(
            scan_change,
            ScanChangeRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                ref=commit_sha,
                repo_root=impl.workspace_path,
            ),
            start_to_close_timeout=scan_timeout,
            retry_policy=retry,
        )

        ci_status = "success" if outcome.ci_status == "passed" else (outcome.ci_status or "unknown")
        decision: EvaluatePromotionResult = await workflow.execute_activity(
            evaluate_promotion,
            EvaluatePromotionRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                jira_project=params.jira_project or _project_from_issue(params.issue_key),
                repository=params.repo,
                ci_status=ci_status,
                blocking_findings=scan.blocking_count,
                scan_completed=True,
                plan_approved=params.plan_approved,
                repair_attempts=outcome.repair_attempts,
                max_repair_attempts=params.max_repair_attempts,
                jira_snapshot_current=params.jira_snapshot_current,
                acceptance_verified=outcome.acceptance_verdict == "pass",
                pr_authorized=outcome.pr_number > 0,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        event_type = "POLICY_EVALUATED" if decision.allow else "POLICY_DENIED"
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:{event_type}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type=event_type,
                stage="policy",
                new_state=POLICY_EVALUATION,
                workflow_id=workflow_id,
                decision=decision.next_action,
                reason=",".join(decision.reasons),
                artifact_hashes={"bundle_version": decision.bundle_version},
            )
        )

        return replace(
            outcome,
            status=POLICY_ALLOWED if decision.allow else POLICY_DENIED,
            policy_status="allowed" if decision.allow else "denied",
            policy_bundle_version=decision.bundle_version,
            policy_next_action=decision.next_action,
            policy_reasons=list(decision.reasons),
            scan_blocking_findings=scan.blocking_count,
        )

    async def _review_gate(
        self,
        params: DraftPullRequestInput,
        gate: Any,
        state: str,
        subject: str,
        commit_sha: str,
        impl: ImplementationOutput,
        workflow_id: str,
    ) -> ReviewOutcome:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        model_timeout = timedelta(minutes=5)

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=state,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        outcome: ReviewOutcome = await workflow.execute_activity(
            gate,
            ReviewRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                subject=subject,
                context_ref=commit_sha,
                repo_root=impl.workspace_path or None,
                ref=commit_sha or None,
            ),
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:REVIEW_COMPLETED:{state}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="REVIEW_COMPLETED",
                stage=state,
                prev_state=state,
                new_state=state,
                model_id=outcome.model_id,
                decision=outcome.verdict,
                prompt_version=outcome.prompt_version,
                artifact_hashes={"attestation": outcome.attestation_hash},
                workflow_id=workflow_id,
            )
        )
        return outcome

    async def _audit(self, event: AuditEvent) -> None:
        await workflow.execute_activity(
            record_audit_event,
            event,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
