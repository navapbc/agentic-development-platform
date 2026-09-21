"""Temporal orchestrator that drives a bounded task from claim to draft PR and Jira linkback."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from adapters.interfaces.git_host import PublishResult

    from services.orchestration.change_narrative import (
        PullRequestNarrative,
        compose_pull_request_body,
        compose_testing_evidence,
    )
    from services.orchestration.gates.contract import PAUSE_FOR_HUMAN, GateContext
    from services.orchestration.gates.runner import run_gate_stage
    from services.orchestration.intake_base import declared_issue_key
    from services.orchestration.temporal_activities.authorship import (
        SignAuthorshipRequest,
        VerifyAuthorshipRequest,
        sign_authorship,
        verify_authorship_merge,
    )
    from services.orchestration.temporal_activities.branch_sync import (
        RecheckRequest,
        RecheckResult,
        recheck_issue,
    )
    from services.orchestration.temporal_activities.checks import (
        RunChecksRequest,
        RunChecksResult,
        run_checks,
    )
    from services.orchestration.temporal_activities.dependency import (
        CaptureBaseHeadRequest,
        CaptureBaseHeadResult,
        CheckDependencyCycleRequest,
        CheckDependencyCycleResult,
        DetectDependencyRequest,
        DetectDependencyResult,
        SignalDependencyReadyRequest,
        capture_base_head,
        check_dependency_cycle,
        detect_dependency,
        signal_dependency_ready,
    )
    from services.orchestration.temporal_activities.gates import (
        ClassifyTestFailuresRequest,
        EvaluateGateRequest,
        GatePlan,
        TriageResult,
        classify_test_failures,
        evaluate_gate,
        load_gate_plan,
    )
    from services.orchestration.temporal_activities.implementation import (
        ResetCoordinationRequest,
        reset_task_coordination,
    )
    from services.orchestration.temporal_activities.manifest import (
        RecordManifestRequest,
        record_manifest,
    )
    from services.orchestration.temporal_activities.notify import (
        AlertHumanInterventionRequest,
        alert_human_intervention,
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
    from services.orchestration.temporal_activities.policy import (
        EvaluatePromotionRequest,
        EvaluatePromotionResult,
        evaluate_promotion,
    )
    from services.orchestration.temporal_activities.pull_request import (
        ApplyStatusRequest,
        CommitPushRequest,
        CommitPushResult,
        LinkbackRequest,
        LinkbackResult,
        ParkTaskRequest,
        ParkTaskResult,
        PromotePrRequest,
        PromotePrResult,
        ReadStatusRequest,
        ReadStatusResult,
        RecordApprovalRequest,
        RecordDependencyLinkRequest,
        UnparkTaskRequest,
        apply_task_status,
        commit_and_push,
        link_pr_to_issue,
        mark_task_parked,
        promote_pr,
        read_ticket_status,
        record_dependency_link,
        record_pr_approval,
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
    from services.orchestration.temporal_activities.security import (
        ScanChangeRequest,
        ScanChangeResult,
        ScanFindingRecord,
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
        COST_BUDGET_EXCEEDED,
        EXISTING_PR_REUSED,
        LLM_CALLS_EXCEEDED,
        TOKEN_BUDGET_EXCEEDED,
        ImplementationInput,
        ImplementationOutput,
        ImplementationWorkflow,
    )
    from services.orchestration.temporal_workflows.pull_request import (
        PullRequestInput,
        PullRequestOutput,
        PullRequestWorkflow,
    )
    from services.orchestration.temporal_workflows.review import (
        PlanReviewInput,
        PlanReviewOutput,
        PlanReviewWorkflow,
    )
    from services.orchestration.terminal_guard import (
        CompletionInputs,
        evaluate_completion,
    )

from adapters.interfaces.policy_engine import PolicyGate

from services.orchestration.suspension import ResumeOn, SuspendReason, Suspension
from services.orchestration.temporal_workflows.suspendable import (
    SuspendableWorkflow,
    is_code_defect,
)

CLAIM_DENIED = "claim_denied"
IMPL_FAILED = "impl_failed"
PLAN_REJECTED = "plan_rejected"
SYNC_PAUSED = "sync_paused"
BRANCH_READY = "branch_ready"
DRAFT_PR_OPEN = "draft_pr_open"
CI_PASSED = "ci_passed"
CI_FAILED = "ci_failed"
CHECKS_RUNNING = "CHECKS_RUNNING"
REPAIRING = "REPAIRING"
REVIEW_PASSED = "review_passed"
REVIEW_BLOCKED = "review_blocked"
POLICY_ALLOWED = "policy_allowed"
POLICY_DENIED = "policy_denied"
POLICY_EVALUATION = "POLICY_EVALUATION"
PR_PROMOTED = "pr_promoted"
PROMOTION_CANCELLED = "promotion_cancelled"
AWAITING_PR_APPROVAL = "AWAITING_PR_APPROVAL"
PR_CREATED = "PR_CREATED"
AWAITING_PR_CLOSE = "AWAITING_PR_CLOSE"
AWAITING_DEPENDENCY = "AWAITING_DEPENDENCY"
DEPENDENCY_CYCLE = "dependency_cycle"
DEPENDENCY_UNRESOLVED = "dependency_replans_exhausted"
AWAITING_HUMAN = "AWAITING_HUMAN"
HUMAN_ABANDONED = "human_abandoned"
CANCELLED = "CANCELLED"
COMPLETED = "COMPLETED"
PR_MERGED = "pr_merged"
PR_CLOSED_UNMERGED = "pr_closed_unmerged"

_SEQUENCE_STRIDE = 100
_COMMIT_OFFSET = 2
_REPAIR_BASE_OFFSET = 3
_CI_TRIGGER_OFFSET = 4
_REPAIR_STRIDE = 2
_APPROVAL_OFFSET = 1
_PROMOTE_OFFSET = 2

# A commit clones the target repository. On an established repository that transfer has overrun
# a five-minute budget under load, and the sequence gate discards every retry that follows.
_COMMIT_TIMEOUT = timedelta(minutes=15)

_CODE_REVIEW = "code-review"
_SECURITY_REVIEW = "security-review"
_ACCEPTANCE = "acceptance"
_REVIEW = "REVIEW"
_REVIEW_KIND = "review"


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
    task_description: str = ""
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
    checks_timeout_minutes: int = 25
    max_repair_attempts: int = 5
    run_reviews: bool = False
    run_planning: bool = False
    run_policy: bool = False
    run_test_triage: bool = False
    run_approval: bool = False
    run_dependency_detection: bool = False
    max_dependency_replans: int = 3
    await_pr_close: bool = False
    jira_project: str = ""
    snapshot_version: int = 0
    plan_approved: bool = False
    jira_snapshot_current: bool = True
    review_task_queue: str = ""


@dataclass
class DraftPullRequestOutput:
    execution_id: str
    status: str
    terminal_reason: str = ""
    plan_status: str = ""
    plan_verdict: str = ""
    branch: str = ""
    pr_number: int = 0
    pr_url: str = ""
    linkback: str = "none"
    reused: bool = False
    ci_status: str = ""
    repair_attempts: int = 0
    review_status: str = ""
    code_verdict: str = ""
    security_verdict: str = ""
    acceptance_verdict: str = ""
    review_verdicts: dict[str, str] = field(default_factory=dict)
    review_attestations: dict[str, str] = field(default_factory=dict)
    review_findings: dict[str, list[str]] = field(default_factory=dict)
    review_summaries: dict[str, str] = field(default_factory=dict)
    policy_status: str = ""
    policy_bundle_version: str = ""
    policy_next_action: str = ""
    policy_reasons: list[str] = field(default_factory=list)
    scan_blocking_findings: int = 0
    scan_findings: list[dict[str, str]] = field(default_factory=list)
    scan_severity_counts: dict[str, int] = field(default_factory=dict)
    approval_status: str = ""
    approver: str = ""
    promoted: bool = False
    pr_merged: bool = False
    pr_close_status: str = ""


@dataclass(frozen=True)
class _PrApproval:
    approved: bool
    approver: str = ""


@dataclass(frozen=True)
class _PrClosed:
    merged: bool


@dataclass(frozen=True)
class _RepairDecision:
    retry: bool
    actor: str = ""


@dataclass(frozen=True)
class _GateDecision:
    gate: str
    approved: bool
    actor: str = ""


@dataclass
class _PlanResult:
    rejected: bool
    output: DraftPullRequestOutput | None = None
    plan_text: str = ""
    context_summary: str = ""
    context_package: dict[str, Any] = field(default_factory=dict)


@dataclass
class _DependencyOutcome:
    plan_text: str
    context_package: dict[str, Any]
    rejection: DraftPullRequestOutput | None = None


def _project_from_issue(issue_key: str) -> str:
    return issue_key.split("-", 1)[0] if "-" in issue_key else ""


def _scanner_unavailable_result(plan: GatePlan) -> ScanChangeResult:
    names = [node.name for node in plan.scanner]
    findings = [
        ScanFindingRecord(
            finding_id=f"{name}-backend-error",
            scanner=name,
            kind="error",
            severity="critical",
            title=f"gate {name!r} backend failed",
            detail="scanner backend unavailable after retries",
            rule_id="gate-backend-error",
        )
        for name in names
    ]
    return ScanChangeResult(
        scanners=names,
        findings=findings,
        blocking_count=len(findings),
        threshold="high",
        content_hash="",
        evidence_id="",
        severity_counts={"critical": len(findings)} if findings else {},
        scanner_verdicts={name: "block" for name in names},
    )


def _acceptance_criteria(params: DraftPullRequestInput) -> str:
    """Resolve the acceptance criteria the completion gate judges against.

    Backends that expose a dedicated criteria field supply it on the stored issue.
    The rebar backend has no such field and carries the criteria inside the ticket
    description, so the description is the fallback.
    """
    stated = str(params.stored_issue.get("acceptance_criteria") or "").strip()
    return stated or params.task_description.strip()


def _change_subject(impl: ImplementationOutput, commit_sha: str) -> str:
    files = "\n".join(impl.changed_files) if impl.changed_files else "(no file list)"
    subject = f"Change on branch {impl.branch} at {commit_sha}.\nChanged files:\n{files}"
    if impl.summary:
        subject = f"{subject}\n\nImplementation summary:\n{impl.summary}"
    return subject


@workflow.defn
class DraftPullRequestWorkflow(SuspendableWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._sync: BranchSyncOutput | None = None
        self._commit: CommitPushResult | None = None
        self._pr: PullRequestOutput | None = None
        self._pr_approval: _PrApproval | None = None
        self._pr_closed: _PrClosed | None = None
        self._repair_decision: _RepairDecision | None = None
        self._gate_decision: _GateDecision | None = None
        self._max_attempt_used: int = 0
        self._repair_cost: float = 0.0
        self._repair_tokens: int = 0
        self._repair_cache_read_tokens: int = 0
        self._repair_llm_calls: int = 0
        self._plan_approved: bool = False
        self._alert_seq: int = 0
        self._checks_output: str = ""
        self._dependency_finding: DetectDependencyResult | None = None
        self._replan_attempts: int = 0
        self._dependency_seq: int = 0
        self._base_sha: str = ""
        self._authorship: dict[str, dict] = {}

    async def _sign_authorship(
        self,
        params: DraftPullRequestInput,
        *,
        kind: str,
        artifact: str,
        commit: str = "",
        force: bool = False,
    ) -> None:
        if kind in self._authorship and not force:
            return
        record = await workflow.execute_activity(
            sign_authorship,
            SignAuthorshipRequest(
                execution_id=params.execution_id, kind=kind, artifact=artifact, commit=commit
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if record is not None:
            self._authorship[kind] = record

    async def _verify_authorship(self) -> None:
        await workflow.execute_activity(
            verify_authorship_merge,
            VerifyAuthorshipRequest(records=list(self._authorship.values())),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    @workflow.signal(name="submit_pr_approval")
    def submit_pr_approval(self, approved: bool, approver: str = "") -> None:
        self._pr_approval = _PrApproval(approved=approved, approver=approver)
        self._resume_trigger = ResumeOn.HUMAN_APPROVAL.value
        self._resume_requested = True

    @workflow.signal(name="submit_completion")
    def submit_completion(self, merged: bool, actor: str = "") -> None:
        self._pr_closed = _PrClosed(merged=merged)
        self._resume_trigger = ResumeOn.PR_CLOSED.value
        self._resume_requested = True

    @workflow.signal(name="submit_pr_closed")
    def submit_pr_closed(self, merged: bool) -> None:
        self.submit_completion(merged)

    @workflow.signal(name="submit_repair_decision")
    def submit_repair_decision(self, retry: bool, actor: str = "") -> None:
        self._repair_decision = _RepairDecision(retry=retry, actor=actor)
        self._resume_trigger = ResumeOn.HUMAN_APPROVAL.value
        self._resume_requested = True

    @workflow.signal(name="submit_gate_decision")
    def submit_gate_decision(self, gate: str, approved: bool, actor: str = "") -> None:
        self._gate_decision = _GateDecision(gate=gate, approved=approved, actor=actor)
        self._resume_trigger = ResumeOn.HUMAN_APPROVAL.value
        self._resume_requested = True

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        workflow_id = workflow.info().workflow_id

        tracker_key = declared_issue_key(params.stored_issue.get("labels") or ())
        default_title = (
            f"{tracker_key}: {params.task}"
            if tracker_key
            else f"[{params.task_id}] {params.task}"
        )
        title = params.title or default_title

        await workflow.execute_activity(
            record_manifest,
            RecordManifestRequest(
                execution_id=params.execution_id,
                task_id=params.task_id,
                repository_sha=params.base_sha,
                snapshot_version=params.snapshot_version,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

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

        self._plan_approved = params.plan_approved
        self._base_sha = params.base_sha
        plan_text = ""
        context_package: dict[str, Any] = {}
        if params.run_planning:
            plan_result = await self._produce_plan(params, workflow_id)
            if plan_result.rejected and plan_result.output is not None:
                return plan_result.output
            plan_text = plan_result.plan_text
            context_package = plan_result.context_package

        if params.run_dependency_detection and context_package:
            outcome = await self._resolve_dependencies(
                params, workflow_id, plan_text, context_package
            )
            if outcome.rejection is not None:
                return outcome.rejection
            plan_text = outcome.plan_text
            context_package = outcome.context_package

        await self._sign_authorship(
            params, kind="plan", artifact=plan_text or params.task, commit=self._base_sha
        )

        impl: ImplementationOutput = await workflow.execute_child_workflow(
            ImplementationWorkflow.run,
            ImplementationInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                commit=self._base_sha,
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
                plan=plan_text,
                context_package=context_package,
            ),
            id=f"{workflow_id}-impl",
        )

        if impl.status == EXISTING_PR_REUSED:
            action = await linkback_existing(impl.pr_url, impl.branch)
            await self._terminal_alert(
                params,
                workflow_id,
                reason=EXISTING_PR_REUSED,
                stage="implementation",
                detail=impl.pr_url,
            )
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
            await self._terminal_alert(
                params,
                workflow_id,
                reason=CLAIM_DENIED,
                stage="claim",
                detail=impl.terminal_reason,
            )
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=CLAIM_DENIED,
                terminal_reason=impl.terminal_reason,
            )

        if impl.status != "implemented":
            await self._terminal_alert(
                params,
                workflow_id,
                reason=impl.terminal_reason or IMPL_FAILED,
                stage="implementation",
                detail=impl.terminal_reason,
            )
            return DraftPullRequestOutput(
                execution_id=params.execution_id,
                status=IMPL_FAILED,
                terminal_reason=impl.terminal_reason,
                branch=impl.branch,
            )

        if params.issue_key:
            await workflow.execute_activity(
                apply_task_status,
                ApplyStatusRequest(
                    issue_key=params.issue_key,
                    stage="work_started",
                    execution_id=params.execution_id,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        while True:
            try:
                outcome = await self._push_branch(params, impl, workflow_id)
                break
            except Exception as exc:  # noqa: BLE001
                if not is_code_defect(exc):
                    raise
                suspension = Suspension(
                    reason=SuspendReason.AWAITING_FIX,
                    resume_on=ResumeOn.NEWER_BUILD,
                    stage="push_branch",
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

        if outcome.status == BRANCH_READY:
            outcome = await self._ci_and_repair(params, impl, outcome, workflow_id)
            while outcome.status == CI_FAILED:
                await self._alert_human_intervention(params, outcome, workflow_id)
                decision = await self._await_repair_decision(params, outcome, workflow_id)
                if not decision.retry:
                    commit_sha = self._commit.commit_sha if self._commit else params.base_sha
                    await workflow.execute_activity(
                        persist_execution_state,
                        ExecutionState(
                            execution_id=params.execution_id,
                            task_id=params.task_id,
                            state=CANCELLED,
                            workflow_id=workflow_id,
                            repository=params.repo or None,
                            repository_sha=commit_sha or None,
                        ),
                        start_to_close_timeout=short,
                        retry_policy=retry,
                    )
                    outcome = replace(outcome, terminal_reason=HUMAN_ABANDONED)
                    break
                outcome = await self._ci_and_repair(
                    params, impl, outcome, workflow_id, base=self._max_attempt_used + 1
                )
            if outcome.status == CI_PASSED:
                passed = True
                if params.run_reviews:
                    outcome = await self._review(params, impl, outcome, workflow_id)
                    passed = outcome.status == REVIEW_PASSED
                if passed and params.run_policy:
                    outcome = await self._policy(params, impl, outcome, workflow_id)
                    passed = outcome.status == POLICY_ALLOWED
                if passed:
                    outcome = await self._open_pull_request(
                        params, impl, outcome, workflow_id, title
                    )
                    return await self._promote(params, impl, outcome, workflow_id)
        return outcome

    async def _push_branch(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        commit_timeout = _COMMIT_TIMEOUT
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
            await self._terminal_alert(
                params,
                workflow_id,
                reason=sync.terminal_reason or SYNC_PAUSED,
                stage="branch_sync",
                detail=sync.terminal_reason,
            )
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
                    file_impact=list(params.file_impact),
                ),
                start_to_close_timeout=commit_timeout,
                retry_policy=retry,
            )
        commit = self._commit

        return DraftPullRequestOutput(
            execution_id=params.execution_id,
            status=BRANCH_READY,
            terminal_reason=impl.terminal_reason,
            branch=commit.branch,
        )

    @staticmethod
    def _testing_evidence(params: DraftPullRequestInput) -> str:
        return compose_testing_evidence(
            reviewed=params.run_reviews, policy_evaluated=params.run_policy
        )

    def _post_repair_sequence_base(
        self, params: DraftPullRequestInput, impl: ImplementationOutput
    ) -> int:
        """The first sequence that clears every repair commit this run could have made.

        The gate admits a sequence only when it exceeds the one before it, so each stage has
        to outrank the stages that already ran. Repair commits occupy
        ``_REPAIR_BASE_OFFSET + _REPAIR_STRIDE * attempt`` and all of them land before the pull
        request opens, so opening the PR and promoting it both start from here.
        """
        attempts = max(params.max_repair_attempts, self._max_attempt_used)
        return (
            impl.claim_version * _SEQUENCE_STRIDE
            + _CI_TRIGGER_OFFSET
            + _REPAIR_STRIDE * attempts
        )

    async def _open_pull_request(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
        title: str,
    ) -> DraftPullRequestOutput:
        commit = self._commit
        commit_branch = commit.branch if commit else outcome.branch

        body = params.body or compose_pull_request_body(
            commit.pr_template if commit else "",
            PullRequestNarrative(
                issue_key=declared_issue_key(params.stored_issue.get("labels") or ())
                or params.issue_key,
                execution_id=params.execution_id,
                summary=impl.summary or params.task,
                description=params.task_description,
                testing=self._testing_evidence(params),
            ),
        )
        if self._pr is None:
            self._pr = await workflow.execute_child_workflow(
                PullRequestWorkflow.run,
                PullRequestInput(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    repo=params.repo,
                    base=params.base,
                    branch=commit_branch,
                    title=title,
                    body=body,
                    issue_key=params.issue_key,
                    base_sha=params.base_sha,
                    sequence=self._post_repair_sequence_base(params, impl),
                ),
                id=f"{workflow_id}-pr",
            )
        pr = self._pr

        return replace(
            outcome,
            status=DRAFT_PR_OPEN,
            branch=commit_branch,
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
        base: int = 0,
    ) -> DraftPullRequestOutput:
        result = await self._run_ci(params, impl, outcome, workflow_id, attempt=base)
        repairs = base
        limit = base + params.max_repair_attempts
        budget_reason = ""
        flaky_reran = False
        while result.status == CI_FAILED and repairs < limit:
            if params.run_test_triage:
                action = await self._triage_ci_failure(params, impl, result, workflow_id)
                if action == "rerun" and not flaky_reran:
                    flaky_reran = True
                    result = await self._run_ci(params, impl, result, workflow_id, attempt=repairs)
                    continue
                if action in ("escalate", "rerun"):
                    break
            attempt = repairs + 1
            repair = await self._repair(params, impl, result, workflow_id, attempt)
            if repair is None:
                break
            budget_reason = self._budget_breach(params, impl)
            if repair.applied:
                repairs = attempt
                flaky_reran = False
            if budget_reason or not repair.applied:
                break
            result = await self._run_ci(params, impl, result, workflow_id, attempt=repairs)

        self._max_attempt_used = max(self._max_attempt_used, repairs)
        if budget_reason:
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:REPAIR_BUDGET_EXCEEDED:{repairs}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="REPAIR_BUDGET_EXCEEDED",
                    stage="repair",
                    new_state=REPAIRING,
                    workflow_id=workflow_id,
                    reason=budget_reason,
                )
            )
            return replace(result, repair_attempts=repairs, terminal_reason=budget_reason)
        if result.status == CI_FAILED:
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:REPAIR_EXHAUSTED:{repairs}",
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

    def _budget_breach(self, params: DraftPullRequestInput, impl: ImplementationOutput) -> str:
        total_cost = impl.cost + self._repair_cost
        total_tokens = impl.tokens + self._repair_tokens
        cached_tokens = impl.cache_read_tokens + self._repair_cache_read_tokens
        billable_tokens = max(0, total_tokens - cached_tokens)
        total_llm_calls = impl.llm_calls + self._repair_llm_calls
        if total_llm_calls > params.max_llm_calls:
            return LLM_CALLS_EXCEEDED
        if billable_tokens > params.max_tokens:
            return TOKEN_BUDGET_EXCEEDED
        if total_cost > params.max_cost_usd:
            return COST_BUDGET_EXCEEDED
        return ""

    async def _terminal_alert(
        self,
        params: DraftPullRequestInput,
        workflow_id: str,
        *,
        reason: str,
        stage: str,
        detail: str = "",
    ) -> None:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        await workflow.execute_activity(
            alert_human_intervention,
            AlertHumanInterventionRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                workflow_id=workflow_id,
                repo=params.repo,
                issue_key=params.issue_key,
                reason=reason,
                stage=stage,
                detail=detail,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        self._alert_seq += 1
        await self._audit(
            AuditEvent(
                event_id=(
                    f"{params.execution_id}:HUMAN_INTERVENTION_ALERTED"
                    f":{stage}:{self._alert_seq}"
                ),
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="HUMAN_INTERVENTION_ALERTED",
                stage=stage,
                workflow_id=workflow_id,
                reason=reason,
            )
        )
        await workflow.execute_activity(
            reset_task_coordination,
            ResetCoordinationRequest(
                task_id=params.task_id,
                work_unit=params.work_unit,
                workflow_id=workflow_id,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

    async def _alert_human_intervention(
        self,
        params: DraftPullRequestInput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> None:
        failure_summary = self._checks_output
        reason = "repair_exhausted"
        if outcome.terminal_reason in (
            COST_BUDGET_EXCEEDED,
            TOKEN_BUDGET_EXCEEDED,
            LLM_CALLS_EXCEEDED,
        ):
            reason = outcome.terminal_reason
        await self._terminal_alert(
            params,
            workflow_id,
            reason=reason,
            stage="repair",
            detail=failure_summary,
        )

    async def _await_repair_decision(
        self,
        params: DraftPullRequestInput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> _RepairDecision:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha
        attempts = outcome.repair_attempts
        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=AWAITING_HUMAN,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        requested_id = f"{params.execution_id}:HUMAN_INTERVENTION_REQUESTED:{attempts}"
        await self._audit(
            AuditEvent(
                event_id=requested_id,
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="HUMAN_INTERVENTION_REQUESTED",
                stage="repair",
                new_state=AWAITING_HUMAN,
                workflow_id=workflow_id,
                reason=str(attempts),
            )
        )
        self._repair_decision = None
        await self.await_resume(
            Suspension(
                reason=SuspendReason.AWAITING_HUMAN,
                resume_on=ResumeOn.HUMAN_APPROVAL,
                stage="repair",
                parked_build_id=params.build_id,
            )
        )
        decision = self._repair_decision or _RepairDecision(retry=False)
        self._repair_decision = None
        decided_id = f"{params.execution_id}:HUMAN_INTERVENTION_DECISION:{attempts}"
        await self._audit(
            AuditEvent(
                event_id=decided_id,
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="HUMAN_INTERVENTION_DECISION",
                stage="repair",
                new_state=AWAITING_HUMAN,
                workflow_id=workflow_id,
                decision="retry" if decision.retry else "abandon",
                reason=decision.actor,
            )
        )
        return decision

    async def _park_for_gate_decision(
        self,
        params: DraftPullRequestInput,
        workflow_id: str,
        *,
        gate: str,
        stage: str,
        detail: str = "",
        parked_build_id: str = "",
    ) -> bool:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha
        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=AWAITING_HUMAN,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        await self._terminal_alert(
            params,
            workflow_id,
            reason=f"gate_paused:{gate}",
            stage=stage,
            detail=detail,
        )
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:HUMAN_INTERVENTION_REQUESTED:{stage}:{gate}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="HUMAN_INTERVENTION_REQUESTED",
                stage=stage,
                new_state=AWAITING_HUMAN,
                workflow_id=workflow_id,
                reason=gate,
            )
        )
        self._gate_decision = None
        await self.await_resume(
            Suspension(
                reason=SuspendReason.AWAITING_HUMAN,
                resume_on=ResumeOn.HUMAN_APPROVAL,
                stage=stage,
                parked_build_id=parked_build_id,
            )
        )
        decision = self._gate_decision or _GateDecision(gate=gate, approved=False)
        self._gate_decision = None
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:HUMAN_INTERVENTION_DECISION:{stage}:{gate}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="HUMAN_INTERVENTION_DECISION",
                stage=stage,
                new_state=AWAITING_HUMAN,
                workflow_id=workflow_id,
                decision="approved" if decision.approved else "rejected",
                reason=decision.actor,
            )
        )
        return decision.approved

    async def _triage_ci_failure(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> str:
        retry = RetryPolicy(maximum_attempts=3)
        model_timeout = timedelta(minutes=10)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha

        triage: TriageResult = await workflow.execute_activity(
            classify_test_failures,
            ClassifyTestFailuresRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                ref=commit_sha,
                repo_root=impl.workspace_path,
            ),
            start_to_close_timeout=model_timeout,
            retry_policy=retry,
        )
        if not triage.enabled:
            return ""
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:TEST_TRIAGE:{params.build_id}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="TEST_TRIAGE",
                stage="repair",
                new_state=REPAIRING,
                workflow_id=workflow_id,
                decision=triage.action,
                reason=triage.summary,
            )
        )
        return triage.action

    async def _repair(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
        attempt: int,
    ) -> ApplyCIRepairResult | None:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        commit_timeout = _COMMIT_TIMEOUT
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
                decision=params.build_id,
            )
        )

        plan: AnalyzeCIFailureResult = await workflow.execute_activity(
            analyze_ci_failure,
            AnalyzeCIFailureRequest(failure_summary=self._checks_output, repo=params.repo),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        if not plan.actionable:
            return None

        repair: ApplyCIRepairResult = await workflow.execute_activity(
            apply_ci_repair,
            ApplyCIRepairRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                base=params.base,
                commit=commit_sha,
                instruction=plan.instruction,
                failure_summary=self._checks_output,
                changed_paths=tuple(params.file_impact),
            ),
            start_to_close_timeout=timedelta(seconds=params.activity_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        self._repair_cost += repair.cost
        self._repair_tokens += repair.tokens
        self._repair_cache_read_tokens += repair.cache_read_tokens
        self._repair_llm_calls += repair.llm_calls
        await workflow.execute_activity(
            record_usage_metrics,
            UsageMetrics(
                execution_id=params.execution_id,
                task_id=params.task_id,
                stage="REPAIRING",
                workflow_id=workflow_id,
                total_tokens=self._repair_tokens,
                cache_read_tokens=self._repair_cache_read_tokens,
                llm_calls=self._repair_llm_calls,
                cost_usd=self._repair_cost,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        if not repair.applied:
            return repair

        sequence = (
            impl.claim_version * _SEQUENCE_STRIDE + _REPAIR_BASE_OFFSET + _REPAIR_STRIDE * attempt
        )
        self._commit = await workflow.execute_activity(
            commit_and_push,
            CommitPushRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                base=params.base,
                commit=commit_sha,
                message=params.commit_message,
                sequence=sequence,
                file_impact=list(params.file_impact),
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
        return repair

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
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=CHECKS_RUNNING,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        result: RunChecksResult = await workflow.execute_activity(
            run_checks,
            RunChecksRequest(
                repo=params.repo,
                execution_id=params.execution_id,
                workspace_path=impl.workspace_path or "",
                commit_sha=commit_sha,
                changed_paths=tuple(params.file_impact),
            ),
            start_to_close_timeout=timedelta(minutes=params.checks_timeout_minutes),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        if not result.ran:
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:CHECKS_SKIPPED:{attempt}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="CHECKS_SKIPPED",
                    stage="checks",
                    new_state=CHECKS_RUNNING,
                    workflow_id=workflow_id,
                    decision="skipped",
                    reason=result.skipped_reason,
                )
            )
            return replace(outcome, status=CI_PASSED, ci_status="skipped")

        self._checks_output = result.output
        passed = result.exit_code == 0
        event_type = "CI_PASSED" if passed else "CI_FAILED"
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:{event_type}:{attempt}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type=event_type,
                stage="checks",
                new_state=CHECKS_RUNNING,
                workflow_id=workflow_id,
                reason=str(result.exit_code),
                decision="passed" if passed else "failed",
            )
        )

        return replace(
            outcome,
            status=CI_PASSED if passed else CI_FAILED,
            ci_status="passed" if passed else "failed",
        )

    async def _review(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        model_timeout = timedelta(minutes=5)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha
        subject = _change_subject(impl, commit_sha)

        plan: GatePlan = await workflow.execute_activity(
            load_gate_plan, start_to_close_timeout=short, retry_policy=retry
        )
        if not plan.review:
            return replace(outcome, status=REVIEW_PASSED, review_status="passed")

        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=_REVIEW,
                workflow_id=workflow_id,
                repository=params.repo or None,
                repository_sha=commit_sha or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

        context = GateContext(
            task_id=params.task_id,
            execution_id=params.execution_id,
            repo=params.repo,
            ref=commit_sha,
            repo_root=impl.workspace_path or "",
            subject=subject,
            acceptance_criteria=_acceptance_criteria(params),
        )

        async def execute(spec: Any, gate_context: GateContext) -> Any:
            return await workflow.execute_activity(
                evaluate_gate,
                EvaluateGateRequest(gate=spec.name, context=gate_context),
                start_to_close_timeout=model_timeout,
                retry_policy=retry,
                task_queue=params.review_task_queue or workflow.info().task_queue,
            )

        results = await run_gate_stage(
            plan.review, context, execute=execute, fail_fast=plan.fail_fast_review
        )

        required = {spec.name: spec.required for spec in plan.review}
        on_block = {spec.name: spec.on_block for spec in plan.review}
        attestations: dict[str, str] = {}
        findings: dict[str, list[str]] = {}
        summaries: dict[str, str] = {}
        verdicts: dict[str, str] = {}
        blocked = False
        for result in results:
            if result.verdict == "cancelled":
                continue
            attestations[result.name] = result.attestation_hash
            findings[result.name] = [finding.title for finding in result.findings]
            summaries[result.name] = result.summary
            verdicts[result.name] = result.verdict
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:REVIEW_COMPLETED:{result.name}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="REVIEW_COMPLETED",
                    stage=_REVIEW,
                    prev_state=_REVIEW,
                    new_state=_REVIEW,
                    model_id=result.metadata.get("model_id", ""),
                    decision=result.verdict,
                    prompt_version=result.metadata.get("prompt_version", ""),
                    artifact_hashes={"attestation": result.attestation_hash},
                    workflow_id=workflow_id,
                )
            )
            if result.kind == _REVIEW_KIND and "total_tokens" in result.metadata:
                await workflow.execute_activity(
                    record_usage_metrics,
                    UsageMetrics(
                        execution_id=params.execution_id,
                        task_id=params.task_id,
                        stage=result.name.upper().replace("-", "_"),
                        workflow_id=workflow_id,
                        model_id=result.metadata.get("model_id") or None,
                        prompt_tokens=int(result.metadata.get("prompt_tokens", 0)),
                        completion_tokens=int(result.metadata.get("completion_tokens", 0)),
                        cache_read_tokens=int(result.metadata.get("cache_read_tokens", 0)),
                        cache_write_tokens=int(result.metadata.get("cache_write_tokens", 0)),
                        total_tokens=int(result.metadata.get("total_tokens", 0)),
                        llm_calls=int(result.metadata.get("llm_calls", 0)),
                        cost_usd=float(result.metadata.get("cost_usd", 0.0)),
                    ),
                    start_to_close_timeout=short,
                    retry_policy=retry,
                )
            if required.get(result.name) and result.verdict == "block":
                blocked = True

        if blocked:
            blocking = sorted(
                name
                for name, verdict in verdicts.items()
                if verdict == "block" and required.get(name)
            )
            paused = [name for name in blocking if on_block.get(name) == PAUSE_FOR_HUMAN]
            fail_closed = [name for name in blocking if on_block.get(name) != PAUSE_FOR_HUMAN]
            if not fail_closed and paused:
                approved = await self._park_for_gate_decision(
                    params,
                    workflow_id,
                    gate=",".join(paused),
                    stage="review",
                    detail=f"paused gates: {', '.join(paused)}",
                    parked_build_id=params.build_id,
                )
                blocked = not approved
                if not approved:
                    await self._terminal_alert(
                        params,
                        workflow_id,
                        reason=REVIEW_BLOCKED,
                        stage="review",
                        detail=f"gate decision rejected: {', '.join(paused)}",
                    )
            else:
                await self._terminal_alert(
                    params,
                    workflow_id,
                    reason=REVIEW_BLOCKED,
                    stage="review",
                    detail=f"blocking gates: {', '.join(blocking)}",
                )

        return replace(
            outcome,
            status=REVIEW_BLOCKED if blocked else REVIEW_PASSED,
            review_status="blocked" if blocked else "passed",
            code_verdict=verdicts.get(_CODE_REVIEW, ""),
            security_verdict=verdicts.get(_SECURITY_REVIEW, ""),
            acceptance_verdict=verdicts.get(_ACCEPTANCE, ""),
            review_verdicts=verdicts,
            review_attestations=attestations,
            review_findings=findings,
            review_summaries=summaries,
        )

    async def _produce_plan(
        self, params: DraftPullRequestInput, workflow_id: str
    ) -> _PlanResult:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        gathered: ContextResult = await workflow.execute_activity(
            gather_repo_context,
            ContextRequest(
                repo=params.repo,
                commit=self._base_sha,
                focus=params.focus,
                detail=params.task_description,
                execution_id=params.execution_id,
                task_id=params.task_id,
            ),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry,
        )
        context_summary = gathered.summary
        context_package = gathered.package
        plan_out: PlanReviewOutput = await workflow.execute_child_workflow(
            PlanReviewWorkflow.run,
            PlanReviewInput(
                task_id=params.task_id,
                execution_id=params.execution_id,
                repo=params.repo,
                commit=self._base_sha,
                focus=params.focus,
                context_summary=context_summary,
                task=params.task,
                description=params.task_description,
                review_task_queue=params.review_task_queue,
            ),
            id=f"{workflow_id}-plan-{self._replan_attempts}",
        )
        if not plan_out.approved:
            await self._terminal_alert(
                params,
                workflow_id,
                reason=PLAN_REJECTED,
                stage="plan",
                detail=plan_out.verdict,
            )
            return _PlanResult(
                rejected=True,
                output=DraftPullRequestOutput(
                    execution_id=params.execution_id,
                    status=PLAN_REJECTED,
                    terminal_reason=PLAN_REJECTED,
                    plan_status="rejected",
                    plan_verdict=plan_out.verdict,
                ),
            )
        self._plan_approved = True
        plan: GatePlan = await workflow.execute_activity(
            load_gate_plan, start_to_close_timeout=short, retry_policy=retry
        )
        if plan.plan_pause:
            approved = await self._park_for_gate_decision(
                params,
                workflow_id,
                gate="plan",
                stage="plan",
                detail="plan approval requested",
            )
            if not approved:
                await self._terminal_alert(
                    params,
                    workflow_id,
                    reason=PLAN_REJECTED,
                    stage="plan",
                    detail="plan rejected by human gate",
                )
                return _PlanResult(
                    rejected=True,
                    output=DraftPullRequestOutput(
                        execution_id=params.execution_id,
                        status=PLAN_REJECTED,
                        terminal_reason=PLAN_REJECTED,
                        plan_status="rejected",
                        plan_verdict=plan_out.verdict,
                    ),
                )
        return _PlanResult(
            rejected=False,
            plan_text=plan_out.plan,
            context_summary=context_summary,
            context_package=context_package,
        )

    async def _recapture_base(
        self, params: DraftPullRequestInput, workflow_id: str
    ) -> str:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        result: CaptureBaseHeadResult = await workflow.execute_activity(
            capture_base_head,
            CaptureBaseHeadRequest(repo=params.repo, base=params.base),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        return result.base_sha or self._base_sha

    async def _resolve_dependencies(
        self,
        params: DraftPullRequestInput,
        workflow_id: str,
        plan_text: str,
        context_package: dict[str, Any],
    ) -> _DependencyOutcome:
        while context_package:
            finding = await self._detect_dependency(params, context_package, workflow_id)
            if not finding.detected:
                return _DependencyOutcome(plan_text, context_package)
            if await self._is_dependency_cycle(params, finding, workflow_id):
                rejection = await self._fail_dependency(
                    params, workflow_id, finding, DEPENDENCY_CYCLE
                )
                return _DependencyOutcome(plan_text, context_package, rejection)
            if self._replan_attempts >= params.max_dependency_replans:
                rejection = await self._fail_dependency(
                    params, workflow_id, finding, DEPENDENCY_UNRESOLVED
                )
                return _DependencyOutcome(plan_text, context_package, rejection)
            await self._record_dependency_link(params, finding, workflow_id)
            await self._dependency_audit(
                params, workflow_id, "DEPENDENCY_RECORDED", "record", finding.blocking_task_id
            )
            await self._park_for_dependency(params, finding, workflow_id)
            self._replan_attempts += 1
            self._base_sha = await self._recapture_base(params, workflow_id)
            await self._dependency_audit(
                params, workflow_id, "DEPENDENCY_REBASED", "rebase", self._base_sha
            )
            replan = await self._produce_plan(params, workflow_id)
            if replan.rejected:
                return _DependencyOutcome(plan_text, context_package, replan.output)
            await self._dependency_audit(
                params, workflow_id, "DEPENDENCY_REPLANNED", "replan", finding.blocking_task_id
            )
            plan_text = replan.plan_text
            context_package = replan.context_package
        return _DependencyOutcome(plan_text, context_package)

    async def _dependency_audit(
        self,
        params: DraftPullRequestInput,
        workflow_id: str,
        event_type: str,
        decision: str,
        reason: str,
    ) -> None:
        self._dependency_seq += 1
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:{event_type}:{self._dependency_seq}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type=event_type,
                stage="dependency",
                workflow_id=workflow_id,
                decision=decision,
                reason=reason,
            )
        )

    async def _is_dependency_cycle(
        self,
        params: DraftPullRequestInput,
        finding: DetectDependencyResult,
        workflow_id: str,
    ) -> bool:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        result: CheckDependencyCycleResult = await workflow.execute_activity(
            check_dependency_cycle,
            CheckDependencyCycleRequest(
                dependent_task_id=params.task_id,
                blocking_issue_key=finding.blocking_task_id,
                blocking_task_id=finding.blocking_task_id,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        return result.is_cycle

    async def _fail_dependency(
        self,
        params: DraftPullRequestInput,
        workflow_id: str,
        finding: DetectDependencyResult,
        reason: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=CANCELLED,
                workflow_id=workflow_id,
                repository=params.repo or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        self._dependency_seq += 1
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:DEPENDENCY_FAILED:{self._dependency_seq}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="DEPENDENCY_FAILED",
                stage="dependency",
                new_state=CANCELLED,
                workflow_id=workflow_id,
                decision=reason,
                reason=finding.blocking_task_id,
            )
        )
        return DraftPullRequestOutput(
            execution_id=params.execution_id,
            status=reason,
            terminal_reason=reason,
        )

    async def _record_dependency_link(
        self,
        params: DraftPullRequestInput,
        finding: DetectDependencyResult,
        workflow_id: str,
    ) -> None:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        await workflow.execute_activity(
            record_dependency_link,
            RecordDependencyLinkRequest(
                dependent_issue_key=params.issue_key,
                blocking_issue_key=finding.blocking_task_id,
                execution_id=params.execution_id,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )

    async def _park_for_dependency(
        self,
        params: DraftPullRequestInput,
        finding: DetectDependencyResult,
        workflow_id: str,
    ) -> None:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        await workflow.execute_activity(
            persist_execution_state,
            ExecutionState(
                execution_id=params.execution_id,
                task_id=params.task_id,
                state=AWAITING_DEPENDENCY,
                workflow_id=workflow_id,
                repository=params.repo or None,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        suspension = Suspension(
            reason=SuspendReason.AWAITING_DEPENDENCY,
            resume_on=ResumeOn.DEPENDENCY_READY,
            stage="dependency",
            detail=finding.blocking_task_id,
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
        self._dependency_seq += 1
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:DEPENDENCY_PAUSED:{self._dependency_seq}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="DEPENDENCY_PAUSED",
                stage="dependency",
                new_state=AWAITING_DEPENDENCY,
                workflow_id=workflow_id,
                decision="pause",
                reason=finding.blocking_task_id,
            )
        )
        await self.await_resume(suspension)
        await workflow.execute_activity(
            clear_suspension,
            ClearSuspensionRequest(workflow_id=workflow_id),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        self._dependency_seq += 1
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:DEPENDENCY_RESUMED:{self._dependency_seq}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="DEPENDENCY_RESUMED",
                stage="dependency",
                workflow_id=workflow_id,
                decision="resume",
                reason=finding.blocking_task_id,
            )
        )

    async def _detect_dependency(
        self,
        params: DraftPullRequestInput,
        context_package: dict[str, Any],
        workflow_id: str,
    ) -> DetectDependencyResult:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        result: DetectDependencyResult = await workflow.execute_activity(
            detect_dependency,
            DetectDependencyRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                issue_key=params.issue_key,
                context_package=context_package,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        self._dependency_finding = result
        if result.detected:
            self._dependency_seq += 1
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:DEPENDENCY_DETECTED:{self._dependency_seq}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="DEPENDENCY_DETECTED",
                    stage="dependency",
                    workflow_id=workflow_id,
                    decision=result.reason,
                    reason=result.blocking_task_id,
                )
            )
        return result

    async def _policy(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        scan_timeout = timedelta(minutes=20)
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

        plan: GatePlan = await workflow.execute_activity(
            load_gate_plan, start_to_close_timeout=short, retry_policy=retry
        )

        try:
            scan: ScanChangeResult = await workflow.execute_activity(
                scan_change,
                ScanChangeRequest(
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    repo=params.repo,
                    ref=commit_sha,
                    repo_root=impl.workspace_path,
                    base_ref=params.base_sha,
                ),
                start_to_close_timeout=scan_timeout,
                retry_policy=retry,
            )
        except ActivityError:
            # The scan could not run after its retries were exhausted. Fail the scanner gates
            # closed rather than crash the workflow, mirroring an in-band backend-error finding.
            scan = _scanner_unavailable_result(plan)
        scan_findings = [
            {
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "scanner": finding.scanner,
                "kind": finding.kind,
                "location": finding.location,
                "title": finding.title,
            }
            for finding in scan.findings
        ]

        gates: list[PolicyGate] = []
        for node in plan.review:
            verdict = outcome.review_verdicts.get(node.name)
            if verdict is not None:
                gates.append(
                    PolicyGate(
                        name=node.name,
                        required=node.required,
                        verdict=verdict,
                        on_block=node.on_block,
                    )
                )
        for node in plan.scanner:
            verdict = scan.scanner_verdicts.get(node.name)
            if verdict is not None:
                gates.append(
                    PolicyGate(
                        name=node.name,
                        required=node.required,
                        verdict=verdict,
                        on_block=node.on_block,
                    )
                )
        required_gates = [
            node.name for node in [*plan.review, *plan.scanner] if node.required
        ]

        ci_status = "success" if outcome.ci_status == "passed" else (outcome.ci_status or "unknown")
        decision: EvaluatePromotionResult = await workflow.execute_activity(
            evaluate_promotion,
            EvaluatePromotionRequest(
                task_id=params.task_id,
                execution_id=params.execution_id,
                jira_project=params.jira_project or _project_from_issue(params.issue_key),
                repository=params.repo,
                ci_status=ci_status,
                repair_attempts=outcome.repair_attempts,
                max_repair_attempts=params.max_repair_attempts,
                jira_snapshot_current=params.jira_snapshot_current,
                pr_authorized=True,
                gates=gates,
                required_gates=required_gates,
                severity_counts=dict(scan.severity_counts),
                scan_findings=scan_findings,
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

        allowed = decision.allow
        if not decision.allow and not decision.reasons and decision.human_review:
            allowed = await self._park_for_gate_decision(
                params,
                workflow_id,
                gate=",".join(decision.human_review),
                stage="policy",
                detail=f"policy human review: {', '.join(decision.human_review)}",
                parked_build_id=params.build_id,
            )
            if not allowed:
                await self._terminal_alert(
                    params,
                    workflow_id,
                    reason=POLICY_DENIED,
                    stage="policy",
                    detail=f"policy human review rejected: {', '.join(decision.human_review)}",
                )
        elif not decision.allow:
            await self._terminal_alert(
                params,
                workflow_id,
                reason=POLICY_DENIED,
                stage="policy",
                detail=",".join(decision.reasons),
            )

        return replace(
            outcome,
            status=POLICY_ALLOWED if allowed else POLICY_DENIED,
            policy_status="allowed" if allowed else "denied",
            policy_bundle_version=decision.bundle_version,
            policy_next_action=decision.next_action,
            policy_reasons=list(decision.reasons),
            scan_blocking_findings=scan.blocking_count,
            scan_findings=scan_findings,
            scan_severity_counts=dict(scan.severity_counts),
        )

    async def _promote(
        self,
        params: DraftPullRequestInput,
        impl: ImplementationOutput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        commit_sha = self._commit.commit_sha if self._commit else params.base_sha
        promotion_base = self._post_repair_sequence_base(params, impl)

        async def persist(state: str) -> None:
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

        approver = ""
        approved = True
        if params.run_approval:
            if params.issue_key:
                status_now: ReadStatusResult = await workflow.execute_activity(
                    read_ticket_status,
                    ReadStatusRequest(issue_key=params.issue_key),
                    start_to_close_timeout=short,
                    retry_policy=retry,
                )
                if status_now.is_terminal:
                    publish = PublishResult(
                        repo=params.repo,
                        branch=outcome.branch,
                        completion_signal_id=str(outcome.pr_number),
                    )
                    decision = evaluate_completion(
                        CompletionInputs(
                            ticket_status_terminal=True,
                            source_control_terminal=True,
                            status_provenance_authenticated=True,
                            gates_passed=True,
                            expected_branch=outcome.branch,
                            expected_completion_signal_id=str(outcome.pr_number),
                            publish_result=publish,
                        )
                    )
                    if decision.complete:
                        await persist(COMPLETED)
                        await self._audit(
                            AuditEvent(
                                event_id=f"{params.execution_id}:APPROVAL_SHORT_CIRCUIT",
                                task_id=params.task_id,
                                execution_id=params.execution_id,
                                event_type="APPROVAL_SHORT_CIRCUIT",
                                stage="approval",
                                new_state=COMPLETED,
                                workflow_id=workflow_id,
                                decision="complete",
                                reason=f"ticket_terminal:{status_now.status}",
                            )
                        )
                        return replace(
                            outcome,
                            status=PR_MERGED,
                            terminal_reason="completed_from_ticket_status",
                            approval_status="approved",
                            pr_merged=True,
                            pr_close_status=COMPLETED,
                        )
            await persist(AWAITING_PR_APPROVAL)
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:PR_APPROVAL_REQUESTED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="PR_APPROVAL_REQUESTED",
                    stage="approval",
                    new_state=AWAITING_PR_APPROVAL,
                    workflow_id=workflow_id,
                    reason=str(outcome.pr_number),
                )
            )

            suspension = Suspension(
                reason=SuspendReason.AWAITING_REVIEW,
                resume_on=ResumeOn.HUMAN_APPROVAL,
                stage="approval",
                parked_build_id=params.build_id,
            )
            await self.await_resume(suspension)
            approval = self._pr_approval
            self._pr_approval = None
            approver = approval.approver if approval else ""
            approved = bool(approval and approval.approved)

            await workflow.execute_activity(
                record_pr_approval,
                RecordApprovalRequest(
                    repo=params.repo,
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    number=outcome.pr_number,
                    approved=approved,
                    approver=approver,
                    sequence=promotion_base + _APPROVAL_OFFSET,
                    issue_key=params.issue_key,
                    project=_project_from_issue(params.issue_key),
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            await self._audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:PR_APPROVAL_RECORDED",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="PR_APPROVAL_RECORDED",
                    stage="approval",
                    new_state=AWAITING_PR_APPROVAL,
                    workflow_id=workflow_id,
                    decision="approved" if approved else "rejected",
                    reason=approver,
                )
            )

            if not approved:
                await persist(CANCELLED)
                await self._audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:PROMOTION_CANCELLED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="PROMOTION_CANCELLED",
                        stage="approval",
                        new_state=CANCELLED,
                        workflow_id=workflow_id,
                        decision="cancel",
                        reason="approval_rejected",
                    )
                )
                await self._terminal_alert(
                    params,
                    workflow_id,
                    reason=PROMOTION_CANCELLED,
                    stage="approval",
                    detail="approval_rejected",
                )
                return replace(
                    outcome,
                    status=PROMOTION_CANCELLED,
                    terminal_reason="approval_rejected",
                    approval_status="rejected",
                    approver=approver,
                )

        if params.issue_key:
            recheck: RecheckResult = await workflow.execute_activity(
                recheck_issue,
                RecheckRequest(
                    issue_key=params.issue_key,
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    stored_payload=dict(params.stored_issue),
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            if recheck.requires_pause:
                await persist(CANCELLED)
                await self._audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:ISSUE_RECHECK_PAUSED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="ISSUE_RECHECK_PAUSED",
                        stage="approval",
                        new_state=CANCELLED,
                        workflow_id=workflow_id,
                        decision="pause",
                        reason=recheck.reason,
                    )
                )
                await self._terminal_alert(
                    params,
                    workflow_id,
                    reason=PROMOTION_CANCELLED,
                    stage="approval",
                    detail=recheck.category,
                )
                return replace(
                    outcome,
                    status=PROMOTION_CANCELLED,
                    terminal_reason=recheck.category,
                    approval_status="approved",
                    approver=approver,
                )

        await self._sign_authorship(
            params,
            kind="code",
            artifact=commit_sha or params.base_sha,
            commit=commit_sha,
            force=True,
        )
        await self._verify_authorship()

        ready: PromotePrResult = await workflow.execute_activity(
            promote_pr,
            PromotePrRequest(
                repo=params.repo,
                task_id=params.task_id,
                execution_id=params.execution_id,
                number=outcome.pr_number,
                sequence=promotion_base + _PROMOTE_OFFSET,
            ),
            start_to_close_timeout=short,
            retry_policy=retry,
        )
        await persist(PR_CREATED)
        if params.issue_key:
            await workflow.execute_activity(
                apply_task_status,
                ApplyStatusRequest(
                    issue_key=params.issue_key,
                    stage="ready_for_review",
                    execution_id=params.execution_id,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:PR_CREATED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="PR_CREATED",
                stage="promotion",
                new_state=PR_CREATED,
                workflow_id=workflow_id,
                decision="promote",
                reason=str(ready.number),
                artifact_hashes={"pr_url": ready.url},
            )
        )
        promoted = replace(
            outcome,
            status=PR_PROMOTED,
            terminal_reason="promoted",
            approval_status="approved",
            approver=approver,
            promoted=True,
            pr_url=ready.url or outcome.pr_url,
        )
        if not params.await_pr_close:
            return promoted
        return await self._await_pr_close(params, promoted, workflow_id)

    async def _await_pr_close(
        self,
        params: DraftPullRequestInput,
        outcome: DraftPullRequestOutput,
        workflow_id: str,
    ) -> DraftPullRequestOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)

        async def persist(state: str) -> None:
            await workflow.execute_activity(
                persist_execution_state,
                ExecutionState(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    state=state,
                    workflow_id=workflow_id,
                    repository=params.repo or None,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        await persist(AWAITING_PR_CLOSE)
        await self.await_resume(
            Suspension(
                reason=SuspendReason.AWAITING_PR_CLOSE,
                resume_on=ResumeOn.PR_CLOSED,
                stage="pr_close",
                detail=str(outcome.pr_number),
            )
        )
        closed = self._pr_closed
        self._pr_closed = None
        merged = bool(closed and closed.merged)

        publish = PublishResult(
            repo=params.repo,
            branch=outcome.branch,
            completion_signal_id=str(outcome.pr_number),
        )
        decision = evaluate_completion(
            CompletionInputs(
                ticket_status_terminal=merged,
                source_control_terminal=merged,
                status_provenance_authenticated=True,
                gates_passed=True,
                expected_branch=outcome.branch,
                expected_completion_signal_id=str(outcome.pr_number),
                publish_result=publish,
            )
        )
        complete = decision.complete
        terminal_state = COMPLETED if complete else CANCELLED
        close_status = PR_MERGED if complete else PR_CLOSED_UNMERGED
        await persist(terminal_state)
        if complete and params.issue_key:
            await workflow.execute_activity(
                apply_task_status,
                ApplyStatusRequest(
                    issue_key=params.issue_key,
                    stage="terminal",
                    execution_id=params.execution_id,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )
            if params.run_dependency_detection:
                await workflow.execute_activity(
                    signal_dependency_ready,
                    SignalDependencyReadyRequest(
                        blocking_task_id=params.task_id,
                        blocking_issue_key=params.issue_key,
                    ),
                    start_to_close_timeout=short,
                    retry_policy=retry,
                )
        await self._audit(
            AuditEvent(
                event_id=f"{params.execution_id}:{close_status.upper()}",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type=close_status.upper(),
                stage="pr_close",
                new_state=terminal_state,
                workflow_id=workflow_id,
                decision="merged" if complete else "closed",
                reason=decision.precedence_applied or decision.reason,
            )
        )
        return replace(
            outcome,
            status=close_status,
            terminal_reason=close_status,
            pr_merged=complete,
            pr_close_status=terminal_state,
        )

    async def _audit(self, event: AuditEvent) -> None:
        await workflow.execute_activity(
            record_audit_event,
            event,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
