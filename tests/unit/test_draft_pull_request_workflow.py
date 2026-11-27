"""Composition test: the draft-PR orchestrator chains implementation, sync, commit, and PR."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from openhands.runtime.caps import COMPLETED
from services.coordination.sequencing import (
    ADMITTED,
    InMemorySequenceGate,
    SequencedAction,
)
from services.integration.slack.tokens import ActionToken
from services.operator_remedy import StepKind
from services.orchestration.branch_sync import FAST_FORWARD
from services.orchestration.gates.contract import (
    GateContext,
    GateFinding,
    GateNode,
    GateResult,
)
from services.orchestration.suspension import (
    RESUME_SIGNAL,
    ResumeOn,
    SuspendReason,
)
from services.orchestration.temporal_activities.authorship import (
    SignAuthorshipRequest,
    VerifyAuthorshipRequest,
)
from services.orchestration.temporal_activities.bedrock import ModelRequest, ModelResponse
from services.orchestration.temporal_activities.branch_sync import (
    RecheckRequest,
    RecheckResult,
    ResolveRequest,
    ResolveResult,
    ResyncRequest,
    ResyncResult,
    SyncRequest,
    SyncResult,
)
from services.orchestration.temporal_activities.checks import (
    RunChecksRequest,
    RunChecksResult,
)
from services.orchestration.temporal_activities.dependency import (
    CaptureBaseHeadRequest,
    CaptureBaseHeadResult,
    CheckDependencyCycleRequest,
    CheckDependencyCycleResult,
    DetectDependencyRequest,
    DetectDependencyResult,
    SignalDependencyReadyRequest,
    SignalDependencyReadyResult,
)
from services.orchestration.temporal_activities.gates import (
    ClassifyTestFailuresRequest,
    EvaluateGateRequest,
    GatePlan,
    TriageResult,
)
from services.orchestration.temporal_activities.implementation import (
    ClaimRequest,
    ClaimResult,
    FinalizeClaimRequest,
    FinalizeClaimResult,
    ImplementationRequest,
    ImplementationResult,
    ResetCoordinationRequest,
    ResetCoordinationResult,
)
from services.orchestration.temporal_activities.limits import (
    PeriodAlertThread,
    RunLimits,
    RunLimitsRequest,
    TenantBudgetRequest,
    TenantBudgetState,
)
from services.orchestration.temporal_activities.lockfile import (
    DependencyStepRequest,
    DependencyStepResult,
)
from services.orchestration.temporal_activities.manifest import RecordManifestRequest
from services.orchestration.temporal_activities.model_access import (
    DeleteRunModelKeyRequest,
    DeleteRunModelKeyResult,
)
from services.orchestration.temporal_activities.notify import (
    AlertHumanInterventionRequest,
    AlertHumanInterventionResult,
    ParkPost,
    RefreshParkButtonsRequest,
    RefreshParkButtonsResult,
    SettleParkPostsRequest,
)
from services.orchestration.temporal_activities.persistence import (
    AuditEvent,
    ExecutionState,
    UsageMetrics,
)
from services.orchestration.temporal_activities.planning import (
    ContextRequest,
    ContextResult,
    PlanEvidenceRequest,
    PlanEvidenceResult,
)
from services.orchestration.temporal_activities.policy import (
    EvaluatePromotionRequest,
    EvaluatePromotionResult,
)
from services.orchestration.temporal_activities.pull_request import (
    ApplyStatusRequest,
    ApplyStatusResult,
    CommitPushRequest,
    CommitPushResult,
    FindOpenPrRequest,
    FindOpenPrResult,
    LinkbackRequest,
    LinkbackResult,
    OpenPrRequest,
    OpenPrResult,
    ParkTaskRequest,
    ParkTaskResult,
    PromotePrRequest,
    PromotePrResult,
    ReadStatusRequest,
    ReadStatusResult,
    RecordApprovalRequest,
    RecordApprovalResult,
    RecordDependencyLinkRequest,
    RecordDependencyLinkResult,
    UnparkTaskRequest,
)
from services.orchestration.temporal_activities.repair import (
    AnalyzeCIFailureRequest,
    AnalyzeCIFailureResult,
    ApplyCIRepairRequest,
    ApplyCIRepairResult,
)
from services.orchestration.temporal_activities.review import (
    ReviewOutcome,
    ReviewRequest,
)
from services.orchestration.temporal_activities.security import (
    ScanChangeRequest,
    ScanChangeResult,
    ScanFindingRecord,
)
from services.orchestration.temporal_activities.suspension import (
    ClearSuspensionRequest,
    RecordSuspensionRequest,
)
from services.orchestration.temporal_workflows.branch_sync import BranchSyncWorkflow
from services.orchestration.temporal_workflows.draft_pull_request import (
    _CI_TRIGGER_OFFSET,
    _PROMOTE_OFFSET,
    _REPAIR_STRIDE,
    _SEQUENCE_STRIDE,
    CI_FAILED,
    DEPENDENCY_CYCLE,
    DEPENDENCY_UNRESOLVED,
    EXISTING_PR_REUSED,
    HUMAN_ABANDONED,
    PLAN_REJECTED,
    POLICY_DENIED,
    PR_PROMOTED,
    PROMOTION_CANCELLED,
    REVIEW_BLOCKED,
    RUN_FAILED,
    DraftPullRequestInput,
    DraftPullRequestOutput,
    DraftPullRequestWorkflow,
    ParkDecisionOutcome,
)
from services.orchestration.temporal_workflows.implementation import ImplementationWorkflow
from services.orchestration.temporal_workflows.pull_request import PullRequestWorkflow
from services.orchestration.temporal_workflows.review import PlanReviewWorkflow
from sqlalchemy import String
from temporalio import activity, workflow
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
from tests.unit._draft_pr_park_support import DefectOnceDraftPr

FAILURE = "FAILURE"
SUCCESS = "SUCCESS"


def _make_activities(
    existing_pr: FindOpenPrResult | None,
    claim_version: int,
    commits: list[CommitPushRequest],
    pr_opens: list[OpenPrRequest],
    linkbacks: list[LinkbackRequest],
    parks: list[ParkTaskRequest] | None = None,
    unparks: list[UnparkTaskRequest] | None = None,
    *,
    status_moves: list[ApplyStatusRequest] | None = None,
    ci_triggers: list | None = None,
    ci_result: str = SUCCESS,
    ci_results: list[str] | None = None,
    ci_running_polls: int = 0,
    ci_unavailable_polls: int = 0,
    checks_ran: bool = True,
    repairs: list[ApplyCIRepairRequest] | None = None,
    repair_actionable: bool = True,
    repair_cache_read_tokens: int = 0,
    alerts: list[AlertHumanInterventionRequest] | None = None,
    usage: list[UsageMetrics] | None = None,
    review_requests: list[GateContext] | None = None,
    code_verdict: str = "pass",
    security_verdict: str = "pass",
    acceptance_verdict: str = "pass",
    plan_verdict: str = "pass",
    impl_requests: list[ImplementationRequest] | None = None,
    context_requests: list[ContextRequest] | None = None,
    plan_evidence: list[PlanEvidenceRequest] | None = None,
    scan_requests: list[ScanChangeRequest] | None = None,
    scan_blocking_findings: int = 0,
    scan_result_findings: list[ScanFindingRecord] | None = None,
    scan_result_severity_counts: dict[str, int] | None = None,
    run_limits: RunLimits | None = None,
    limit_reads: list[RunLimitsRequest] | None = None,
    tenant_budget: dict[str, Any] | None = None,
    budget_reads: list[TenantBudgetRequest] | None = None,
    period_thread: PeriodAlertThread | None = None,
    scan_change_raises: bool = False,
    policy_requests: list[EvaluatePromotionRequest] | None = None,
    policy_allow: bool = True,
    policy_reasons: list[str] | None = None,
    approvals: list[RecordApprovalRequest] | None = None,
    promotions: list[PromotePrRequest] | None = None,
    recheck_requires_pause: bool = False,
    recheck_category: str = "",
    pr_template: str = "",
    manifest_requests: list[RecordManifestRequest] | None = None,
    triage_enabled: bool = False,
    triage_action_value: str = "patch",
    review_on_block: dict[str, str] | None = None,
    policy_human_review: list[str] | None = None,
    plan_pause: bool = False,
    authorship_verify_raises: bool = False,
    authorship_active: bool = False,
    authorship_verified: list[list[dict]] | None = None,
    commit_shas: list[str] | None = None,
    model_key_deletions: list[DeleteRunModelKeyRequest] | None = None,
    scan_budget_stop: str = "",
    repair_terminal_reason: str = "",
    persisted_states: list[ExecutionState] | None = None,
    audit_events: list[AuditEvent] | None = None,
    claim_finalizations: list[FinalizeClaimRequest] | None = None,
    commit_results: list[CommitPushResult] | None = None,
    dependency_requests: list[DependencyStepRequest] | None = None,
    dependency_results: list[DependencyStepResult] | None = None,
    resync_requests: list[ResyncRequest] | None = None,
    resync_results: list[ResyncResult] | None = None,
):
    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        if persisted_states is not None:
            persisted_states.append(payload)
        return payload.execution_id

    @activity.defn(name="classify_test_failures")
    async def fake_classify(req: ClassifyTestFailuresRequest) -> TriageResult:
        return TriageResult(enabled=triage_enabled, action=triage_action_value)

    @activity.defn(name="record_manifest")
    async def fake_record_manifest(req: RecordManifestRequest) -> str:
        if manifest_requests is not None:
            manifest_requests.append(req)
        return "manifest-event"

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        if audit_events is not None:
            audit_events.append(event)
        return event.event_id

    @activity.defn(name="record_usage_metrics")
    async def fake_usage(payload: UsageMetrics) -> str:
        if usage is not None:
            usage.append(payload)
        return payload.execution_id

    @activity.defn(name="gather_repo_context")
    async def fake_context(req: ContextRequest) -> ContextResult:
        if context_requests is not None:
            context_requests.append(req)
        return ContextResult(summary="repo context", package={"nodes": ["feature.py"]})

    @activity.defn(name="record_plan_evidence")
    async def fake_plan_evidence(req: PlanEvidenceRequest) -> PlanEvidenceResult:
        if plan_evidence is not None:
            plan_evidence.append(req)
        return PlanEvidenceResult(artifact_id="plan-artifact")

    @activity.defn(name="claim_work_unit")
    async def fake_claim(req: ClaimRequest) -> ClaimResult:
        return ClaimResult(claim_id="claim-1", won=True, claim_version=claim_version)

    @activity.defn(name="find_open_pr")
    async def fake_find_open_pr(req: FindOpenPrRequest) -> FindOpenPrResult:
        return existing_pr or FindOpenPrResult(found=False)

    @activity.defn(name="run_implementation_stage")
    async def fake_stage(req: ImplementationRequest) -> ImplementationResult:
        if impl_requests is not None:
            impl_requests.append(req)
        return ImplementationResult(
            status="finished",
            terminal_reason=COMPLETED,
            tool_calls=1,
            elapsed_seconds=1.0,
            cost=0.1,
            tokens=10,
            llm_calls=1,
            branch="agent/T-1",
            commit_sha="a" * 40,
            diff_hash="sha256:feed",
            model_id="coding-model",
            prompt_version="implementation/v1",
            workspace_path="/tmp/ws/agent-T-1",
            changed_files=["README.md"],
            summary="done",
        )

    @activity.defn(name="synchronize_branch")
    async def fake_sync(req: SyncRequest) -> SyncResult:
        return SyncResult(status=FAST_FORWARD, base_head="b" * 40, human_commits=False)

    @activity.defn(name="resolve_conflicts")
    async def fake_resolve(req: ResolveRequest) -> ResolveResult:
        return ResolveResult(status="resolved")

    recheck_calls: list[int] = []

    @activity.defn(name="recheck_issue")
    async def fake_recheck(req: RecheckRequest) -> RecheckResult:
        recheck_calls.append(1)
        if recheck_requires_pause and len(recheck_calls) >= 2:
            return RecheckResult(
                category=recheck_category, requires_pause=True, reason="issue changed"
            )
        return RecheckResult(category="", requires_pause=False)

    @activity.defn(name="resync_branch")
    async def fake_resync(req: ResyncRequest) -> ResyncResult:
        requested = resync_requests if resync_requests is not None else []
        requested.append(req)
        if not resync_results:
            return ResyncResult(status="current", base_head="b" * 40)
        return resync_results[min(len(requested) - 1, len(resync_results) - 1)]

    @activity.defn(name="prepare_dependencies")
    async def fake_prepare_dependencies(req: DependencyStepRequest) -> DependencyStepResult:
        requested = dependency_requests if dependency_requests is not None else []
        requested.append(req)
        if not dependency_results:
            return DependencyStepResult()
        return dependency_results[min(len(requested) - 1, len(dependency_results) - 1)]

    @activity.defn(name="commit_and_push")
    async def fake_commit(req: CommitPushRequest) -> CommitPushResult:
        commits.append(req)
        if commit_results:
            return commit_results[min(len(commits) - 1, len(commit_results) - 1)]
        sha = "c" * 40
        if commit_shas:
            sha = commit_shas[min(len(commits) - 1, len(commit_shas) - 1)]
        return CommitPushResult(
            branch="agent/T-1",
            commit_sha=sha,
            pushed=True,
            replayed=False,
            pr_template=pr_template,
        )

    @activity.defn(name="create_draft_pr")
    async def fake_create_pr(req: OpenPrRequest) -> OpenPrResult:
        pr_opens.append(req)
        return OpenPrResult(
            number=5,
            url="https://github.com/org/agentic-demo-service/pull/5",
            created=True,
            draft=True,
            state="open",
            orphan_id="orphan-1",
        )

    @activity.defn(name="link_pr_to_issue")
    async def fake_link(req: LinkbackRequest) -> LinkbackResult:
        linkbacks.append(req)
        return LinkbackResult(action="applied", detail="")

    @activity.defn(name="record_suspension")
    async def fake_record_suspension(req: RecordSuspensionRequest) -> str:
        return req.workflow_id

    @activity.defn(name="clear_suspension")
    async def fake_clear_suspension(req: ClearSuspensionRequest) -> str:
        return req.workflow_id

    @activity.defn(name="mark_task_parked")
    async def fake_mark_parked(req: ParkTaskRequest) -> ParkTaskResult:
        if parks is not None:
            parks.append(req)
        return ParkTaskResult(previous_status="In Progress", transitioned=True)

    @activity.defn(name="unmark_task_parked")
    async def fake_unmark_parked(req: UnparkTaskRequest) -> str:
        if unparks is not None:
            unparks.append(req)
        return req.issue_key

    @activity.defn(name="apply_task_status")
    async def fake_apply_status(req: ApplyStatusRequest) -> ApplyStatusResult:
        if status_moves is not None:
            status_moves.append(req)
        return ApplyStatusResult(status=req.stage, transitioned=bool(req.stage))

    @activity.defn(name="read_ticket_status")
    async def fake_read_status(req: ReadStatusRequest) -> ReadStatusResult:
        return ReadStatusResult(status="", is_terminal=False)

    checks_count = {"n": 0}

    @activity.defn(name="run_checks")
    async def fake_run_checks(req: RunChecksRequest) -> RunChecksResult:
        idx = checks_count["n"]
        checks_count["n"] += 1
        outcome = ci_results[min(idx, len(ci_results) - 1)] if ci_results is not None else ci_result
        if ci_triggers is not None:
            ci_triggers.append(req)
        passed = outcome == SUCCESS
        return RunChecksResult(
            ran=checks_ran,
            exit_code=0 if passed else 1,
            output="" if passed else "AssertionError: expected 1 got 2",
            skipped_reason="" if checks_ran else "no checks configured",
        )

    @activity.defn(name="analyze_ci_failure")
    async def fake_analyze(req: AnalyzeCIFailureRequest) -> AnalyzeCIFailureResult:
        if not repair_actionable:
            return AnalyzeCIFailureResult(actionable=False, category="unknown", instruction="")
        return AnalyzeCIFailureResult(
            actionable=True,
            category="test_failure",
            instruction="Fix the failing assertion",
        )

    @activity.defn(name="apply_ci_repair")
    async def fake_apply_repair(req: ApplyCIRepairRequest) -> ApplyCIRepairResult:
        if repairs is not None:
            repairs.append(req)
        return ApplyCIRepairResult(
            applied=True,
            changed_files=["feature.py"],
            cost=0.1,
            tokens=5,
            cache_read_tokens=repair_cache_read_tokens,
            llm_calls=1,
            terminal_reason=repair_terminal_reason,
        )

    @activity.defn(name="alert_human_intervention")
    async def fake_alert(req: AlertHumanInterventionRequest) -> AlertHumanInterventionResult:
        if alerts is not None:
            alerts.append(req)
        return AlertHumanInterventionResult(channels=["jira"])

    @activity.defn(name="reset_task_coordination")
    async def fake_reset(req: ResetCoordinationRequest) -> ResetCoordinationResult:
        return ResetCoordinationResult(sequence_reset=True)

    @activity.defn(name="finalize_work_claim")
    async def fake_finalize(req: FinalizeClaimRequest) -> FinalizeClaimResult:
        if claim_finalizations is not None:
            claim_finalizations.append(req)
        return FinalizeClaimResult(
            claim_id="claim-1",
            finalized=True,
            state="completed" if req.completed else "released",
        )

    @activity.defn(name="read_run_limits")
    async def fake_read_run_limits(req: RunLimitsRequest) -> RunLimits:
        if limit_reads is not None:
            limit_reads.append(req)
        return run_limits or req.configured

    @activity.defn(name="read_tenant_budget")
    async def fake_read_tenant_budget(req: TenantBudgetRequest) -> TenantBudgetState:
        if budget_reads is not None:
            budget_reads.append(req)
        if tenant_budget is None or not tenant_budget.get("spent"):
            return TenantBudgetState(spent=False)
        return TenantBudgetState(spent=True, usage="100 of 100 USD", spend=100.0)

    @activity.defn(name="period_alert_thread")
    async def fake_period_alert_thread(req: TenantBudgetRequest) -> PeriodAlertThread:
        return period_thread or PeriodAlertThread(reason="no period alert in this test")

    @activity.defn(name="delete_run_model_key")
    async def fake_delete_run_model_key(req: DeleteRunModelKeyRequest) -> DeleteRunModelKeyResult:
        if model_key_deletions is not None:
            model_key_deletions.append(req)
        return DeleteRunModelKeyResult(deleted=True)

    def _review_outcome(stage: str, verdict: str) -> ReviewOutcome:
        return ReviewOutcome(
            verdict=verdict,
            attestation_hash=f"sha256:{stage}",
            prompt_version=f"{stage}/v1",
            model_id="review-model",
            summary="reviewed",
            blocking_findings=1 if verdict == "block" else 0,
            findings=[f"{stage} blocking finding"] if verdict == "block" else [],
        )

    @activity.defn(name="load_gate_plan")
    async def fake_load_gate_plan(req=None) -> GatePlan:
        on_block = review_on_block or {}
        return GatePlan(
            review=[
                GateNode(name="code-review", on_block=on_block.get("code-review", "fail_closed")),
                GateNode(
                    name="security-review",
                    on_block=on_block.get("security-review", "fail_closed"),
                ),
                GateNode(name="acceptance", on_block=on_block.get("acceptance", "fail_closed")),
            ],
            scanner=[
                GateNode(name="semgrep"),
                GateNode(name="trivy"),
            ],
            fail_fast_review=False,
            fail_fast_scanner=False,
            plan_pause=plan_pause,
        )

    _gate_verdicts = {
        "code-review": code_verdict,
        "security-review": security_verdict,
        "acceptance": acceptance_verdict,
    }

    @activity.defn(name="evaluate_gate")
    async def fake_evaluate_gate(req: EvaluateGateRequest) -> GateResult:
        if review_requests is not None:
            review_requests.append(req.context)
        verdict = _gate_verdicts[req.gate]
        return GateResult(
            name=req.gate,
            kind="review",
            verdict=verdict,
            attestation_hash=f"sha256:{req.gate}",
            summary="reviewed",
            findings=[GateFinding(severity="block", title=f"{req.gate} blocking finding")]
            if verdict == "block"
            else [],
            metadata={"model_id": "review-model", "prompt_version": f"{req.gate}/v1"},
        )

    @activity.defn(name="invoke_model")
    async def fake_invoke_model(req: ModelRequest) -> ModelResponse:
        return ModelResponse(
            logical_model=req.logical_model,
            content="Plan:\n- Edit feature.py to add the change\n- Update README.md",
            model_id="planning-model",
        )

    @activity.defn(name="review_plan")
    async def fake_review_plan(req: ReviewRequest) -> ReviewOutcome:
        return _review_outcome("plan-review", plan_verdict)

    @activity.defn(name="scan_change")
    async def fake_scan_change(req: ScanChangeRequest) -> ScanChangeResult:
        if scan_requests is not None:
            scan_requests.append(req)
        if scan_change_raises:
            raise RuntimeError("scan backend unavailable")
        findings = list(scan_result_findings or [])
        scanner_verdicts = {"semgrep": "pass", "trivy": "pass"}
        for finding in findings:
            if finding.scanner in scanner_verdicts and finding.severity in {"high", "critical"}:
                scanner_verdicts[finding.scanner] = "block"
        return ScanChangeResult(
            scanners=["semgrep", "trivy"],
            findings=findings,
            blocking_count=scan_blocking_findings,
            threshold="high",
            content_hash="sha256:scan",
            evidence_id="evidence-1",
            severity_counts=dict(scan_result_severity_counts or {}),
            scanner_verdicts=scanner_verdicts,
            budget_stop=scan_budget_stop,
        )

    @activity.defn(name="evaluate_promotion")
    async def fake_evaluate_promotion(req: EvaluatePromotionRequest) -> EvaluatePromotionResult:
        if policy_requests is not None:
            policy_requests.append(req)
        return EvaluatePromotionResult(
            allow=policy_allow,
            bundle_version="0.4.0",
            next_action="proceed" if policy_allow else "block_and_report",
            reasons=list(policy_reasons or []),
            human_review=list(policy_human_review or []),
        )

    @activity.defn(name="record_pr_approval")
    async def fake_record_approval(req: RecordApprovalRequest) -> RecordApprovalResult:
        if approvals is not None:
            approvals.append(req)
        return RecordApprovalResult(recorded=True, approved=req.approved, approver=req.approver)

    @activity.defn(name="promote_pr")
    async def fake_promote(req: PromotePrRequest) -> PromotePrResult:
        if promotions is not None:
            promotions.append(req)
        return PromotePrResult(
            number=req.number,
            url=f"https://github.com/org/agentic-demo-service/pull/{req.number}",
            draft=False,
            state="open",
            ready=True,
        )

    @activity.defn(name="sign_authorship")
    async def fake_sign_authorship(req: SignAuthorshipRequest) -> dict | None:
        if not authorship_active:
            return None
        return {
            "kind": req.kind,
            "identity_id": "id-1",
            "event": {
                "uuid": f"{req.execution_id}:{req.kind}",
                "kind": req.kind,
                "commit": req.commit,
                "artifact_hash": req.artifact,
            },
            "envelope": "{}",
        }

    @activity.defn(name="verify_authorship_merge")
    async def fake_verify_authorship_merge(req: VerifyAuthorshipRequest) -> None:
        if authorship_verified is not None:
            authorship_verified.append(list(req.records))
        if authorship_verify_raises:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError("authorship signature did not verify", non_retryable=True)
        return None

    return [
        fake_persist,
        fake_classify,
        fake_record_manifest,
        fake_audit,
        fake_usage,
        fake_context,
        fake_plan_evidence,
        fake_claim,
        fake_find_open_pr,
        fake_stage,
        fake_sync,
        fake_resolve,
        fake_recheck,
        fake_prepare_dependencies,
        fake_resync,
        fake_commit,
        fake_create_pr,
        fake_link,
        fake_record_suspension,
        fake_clear_suspension,
        fake_mark_parked,
        fake_unmark_parked,
        fake_apply_status,
        fake_read_status,
        fake_run_checks,
        fake_analyze,
        fake_apply_repair,
        fake_alert,
        fake_reset,
        fake_finalize,
        fake_delete_run_model_key,
        fake_read_run_limits,
        fake_read_tenant_budget,
        fake_period_alert_thread,
        fake_load_gate_plan,
        fake_evaluate_gate,
        fake_invoke_model,
        fake_review_plan,
        fake_scan_change,
        fake_evaluate_promotion,
        fake_record_approval,
        fake_promote,
        fake_sign_authorship,
        fake_verify_authorship_merge,
    ]


async def _run(*, existing_pr: FindOpenPrResult | None = None, claim_version: int = 1):
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(existing_pr, claim_version, commits, pr_opens, linkbacks)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, commits, pr_opens, linkbacks


async def test_orchestrator_opens_draft_pr_through_the_full_chain():
    result, commits, pr_opens, linkbacks = await _run()

    assert result.status == PR_PROMOTED
    assert result.branch == "agent/T-1"
    assert result.pr_number == 5
    assert result.pr_url == "https://github.com/org/agentic-demo-service/pull/5"
    assert result.linkback == "applied"
    assert len(commits) == 1
    assert commits[0].sequence == 102
    assert len(pr_opens) == 1
    assert pr_opens[0].sequence == 114
    assert len(linkbacks) == 1


async def test_no_wait_promotion_persists_completed_terminal_state():
    states: list[str] = []

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    activities = [
        activity_fn
        for activity_fn in _make_activities(None, 1, [], [], [])
        if activity_fn.__temporal_activity_definition.name != "persist_execution_state"
    ]
    activities.insert(0, fake_persist)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert states[-2:] == ["PR_CREATED", "COMPLETED"]


async def test_reclaimed_execution_uses_higher_gate_sequences():
    result, commits, pr_opens, _ = await _run(claim_version=2)

    assert result.status == PR_PROMOTED
    assert commits[0].sequence == 202
    assert pr_opens[0].sequence == 214


async def test_orchestrator_reuses_existing_pr_without_committing():
    result, commits, pr_opens, linkbacks = await _run(
        existing_pr=FindOpenPrResult(
            found=True,
            number=7,
            url="https://github.com/org/agentic-demo-service/pull/7",
            title="T-1 draft",
            head="agent/T-1",
        )
    )

    assert result.status == EXISTING_PR_REUSED
    assert result.reused is True
    assert result.pr_number == 7
    assert result.pr_url == "https://github.com/org/agentic-demo-service/pull/7"
    assert commits == []
    assert pr_opens == []
    assert len(linkbacks) == 1


async def test_finalize_code_defect_parks_then_resumes_and_completes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(None, 1, commits, pr_opens, linkbacks)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DefectOnceDraftPr,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DefectOnceDraftPr.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                    build_id="1.4.1",
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            state: dict[str, str] = {}
            for _ in range(50):
                state = await handle.query(DefectOnceDraftPr.suspension_state)
                if state:
                    break
                await asyncio.sleep(0.05)

            assert state["reason"] == SuspendReason.AWAITING_FIX.value
            assert state["resume_on"] == ResumeOn.NEWER_BUILD.value
            assert state["parked_build_id"] == "1.4.1"
            assert state["stage"] == "push_branch"
            assert commits == []

            await handle.signal(RESUME_SIGNAL, ResumeOn.NEWER_BUILD.value)
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert result.pr_number == 5
    assert len(commits) == 1
    assert commits[0].sequence == 102
    assert len(pr_opens) == 1


async def test_dependency_detected_parks_then_resumes_and_completes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(None, 1, commits, pr_opens, linkbacks)
    calls = {"detect": 0, "link": 0, "signal": 0, "recapture": 0}

    @activity.defn(name="detect_dependency")
    async def fake_detect(req: DetectDependencyRequest) -> DetectDependencyResult:
        calls["detect"] += 1
        if calls["detect"] == 1:
            return DetectDependencyResult(
                detected=True,
                active=True,
                blocking_task_id="T-2",
                blocking_execution_id="exec-2",
                reason="file_overlap",
                overlap=["feature.py"],
            )
        return DetectDependencyResult(detected=False, active=True)

    @activity.defn(name="record_dependency_link")
    async def fake_link(req: RecordDependencyLinkRequest) -> RecordDependencyLinkResult:
        calls["link"] += 1
        return RecordDependencyLinkResult(linked=True, detail=req.blocking_issue_key)

    @activity.defn(name="signal_dependency_ready")
    async def fake_signal(req: SignalDependencyReadyRequest) -> SignalDependencyReadyResult:
        calls["signal"] += 1
        return SignalDependencyReadyResult(0)

    @activity.defn(name="capture_base_head")
    async def fake_capture(req: CaptureBaseHeadRequest) -> CaptureBaseHeadResult:
        calls["recapture"] += 1
        return CaptureBaseHeadResult(base_sha="b" * 40)

    @activity.defn(name="check_dependency_cycle")
    async def fake_cycle(req: CheckDependencyCycleRequest) -> CheckDependencyCycleResult:
        return CheckDependencyCycleResult(is_cycle=False)

    activities = [
        *activities,
        fake_detect,
        fake_link,
        fake_signal,
        fake_capture,
        fake_cycle,
    ]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                    run_planning=True,
                    run_dependency_detection=True,
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            state: dict[str, str] = {}
            for _ in range(80):
                state = await handle.query(DraftPullRequestWorkflow.suspension_state)
                if state.get("reason") == SuspendReason.AWAITING_DEPENDENCY.value:
                    break
                await asyncio.sleep(0.05)

            assert state["reason"] == SuspendReason.AWAITING_DEPENDENCY.value
            assert state["resume_on"] == ResumeOn.DEPENDENCY_READY.value
            assert state["detail"] == "T-2"

            await handle.signal(RESUME_SIGNAL, ResumeOn.DEPENDENCY_READY.value)
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert calls["detect"] == 2
    assert calls["link"] == 1
    assert calls["recapture"] == 1


async def test_a_replanned_plan_gate_records_each_decision_under_its_own_park():
    from dataclasses import replace

    audits: list[AuditEvent] = []
    activities = _make_activities(
        None, 1, [], [], [], ci_result=SUCCESS, plan_pause=True, audit_events=audits
    )
    detections = iter(
        [
            DetectDependencyResult(
                detected=True,
                active=True,
                blocking_task_id="T-2",
                blocking_execution_id="exec-2",
                reason="file_overlap",
                overlap=["feature.py"],
            )
        ]
    )

    @activity.defn(name="detect_dependency")
    async def fake_detect(req: DetectDependencyRequest) -> DetectDependencyResult:
        return next(detections, DetectDependencyResult(detected=False, active=True))

    @activity.defn(name="record_dependency_link")
    async def fake_link(req: RecordDependencyLinkRequest) -> RecordDependencyLinkResult:
        return RecordDependencyLinkResult(linked=True, detail=req.blocking_issue_key)

    @activity.defn(name="signal_dependency_ready")
    async def fake_signal(req: SignalDependencyReadyRequest) -> SignalDependencyReadyResult:
        return SignalDependencyReadyResult(0)

    @activity.defn(name="capture_base_head")
    async def fake_capture(req: CaptureBaseHeadRequest) -> CaptureBaseHeadResult:
        return CaptureBaseHeadResult(base_sha="b" * 40)

    @activity.defn(name="check_dependency_cycle")
    async def fake_cycle(req: CheckDependencyCycleRequest) -> CheckDependencyCycleResult:
        return CheckDependencyCycleResult(is_cycle=False)

    activities = [*activities, fake_detect, fake_link, fake_signal, fake_capture, fake_cycle]
    workflow_input = replace(
        _ci_input(run_reviews=True, run_planning=True), run_dependency_detection=True
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                workflow_input,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            first_key = await _await_park_key(handle, "plan")
            await handle.execute_update(
                "apply_gate_decision", args=[first_key, True, "slack:E1/U-FIRST"]
            )
            for _ in range(80):
                state = await handle.query(DraftPullRequestWorkflow.suspension_state)
                if state.get("reason") == SuspendReason.AWAITING_DEPENDENCY.value:
                    break
                await asyncio.sleep(0.05)
            await handle.signal(RESUME_SIGNAL, ResumeOn.DEPENDENCY_READY.value)
            second_key = await _await_park_key(handle, "plan")
            await handle.execute_update(
                "apply_gate_decision", args=[second_key, True, "slack:E1/U-SECOND"]
            )
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert first_key != second_key
    decisions = {decision.event_id: decision.actor_id for decision in _decisions(audits)}
    first, second = (
        ActionToken("alpha", "exec-1", "wf", key, "gate", ("approve",), 0).decision_event_id
        for key in (first_key, second_key)
    )
    assert first != second
    assert (decisions.get(first), decisions.get(second)) == (
        "slack:E1/U-FIRST",
        "slack:E1/U-SECOND",
    )


async def test_dependency_cycle_fails_the_run():
    activities = _make_activities(None, 1, [], [], [])

    @activity.defn(name="detect_dependency")
    async def fake_detect(req: DetectDependencyRequest) -> DetectDependencyResult:
        return DetectDependencyResult(
            detected=True,
            active=True,
            blocking_task_id="T-2",
            blocking_execution_id="exec-2",
            reason="file_overlap",
            overlap=["feature.py"],
        )

    @activity.defn(name="record_dependency_link")
    async def fake_link(req: RecordDependencyLinkRequest) -> RecordDependencyLinkResult:
        return RecordDependencyLinkResult(linked=True)

    @activity.defn(name="signal_dependency_ready")
    async def fake_signal(req: SignalDependencyReadyRequest) -> SignalDependencyReadyResult:
        return SignalDependencyReadyResult(0)

    @activity.defn(name="capture_base_head")
    async def fake_capture(req: CaptureBaseHeadRequest) -> CaptureBaseHeadResult:
        return CaptureBaseHeadResult(base_sha="b" * 40)

    @activity.defn(name="check_dependency_cycle")
    async def fake_cycle(req: CheckDependencyCycleRequest) -> CheckDependencyCycleResult:
        return CheckDependencyCycleResult(is_cycle=True)

    activities = [*activities, fake_detect, fake_link, fake_signal, fake_capture, fake_cycle]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                    run_planning=True,
                    run_dependency_detection=True,
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == DEPENDENCY_CYCLE


async def test_dependency_replans_exhausted_fails_the_run():
    activities = _make_activities(None, 1, [], [], [])

    @activity.defn(name="detect_dependency")
    async def fake_detect(req: DetectDependencyRequest) -> DetectDependencyResult:
        return DetectDependencyResult(
            detected=True,
            active=True,
            blocking_task_id="T-2",
            blocking_execution_id="exec-2",
            reason="file_overlap",
            overlap=["feature.py"],
        )

    @activity.defn(name="record_dependency_link")
    async def fake_link(req: RecordDependencyLinkRequest) -> RecordDependencyLinkResult:
        return RecordDependencyLinkResult(linked=True)

    @activity.defn(name="signal_dependency_ready")
    async def fake_signal(req: SignalDependencyReadyRequest) -> SignalDependencyReadyResult:
        return SignalDependencyReadyResult(0)

    @activity.defn(name="capture_base_head")
    async def fake_capture(req: CaptureBaseHeadRequest) -> CaptureBaseHeadResult:
        return CaptureBaseHeadResult(base_sha="b" * 40)

    @activity.defn(name="check_dependency_cycle")
    async def fake_cycle(req: CheckDependencyCycleRequest) -> CheckDependencyCycleResult:
        return CheckDependencyCycleResult(is_cycle=False)

    activities = [*activities, fake_detect, fake_link, fake_signal, fake_capture, fake_cycle]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                    run_planning=True,
                    run_dependency_detection=True,
                    max_dependency_replans=1,
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            for _ in range(80):
                state = await handle.query(DraftPullRequestWorkflow.suspension_state)
                if state.get("reason") == SuspendReason.AWAITING_DEPENDENCY.value:
                    break
                await asyncio.sleep(0.05)
            await handle.signal(RESUME_SIGNAL, ResumeOn.DEPENDENCY_READY.value)
            result = await handle.result()

    assert result.status == DEPENDENCY_UNRESOLVED


async def test_park_mirrors_jira_status_and_restores_on_resume():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    parks: list[ParkTaskRequest] = []
    unparks: list[UnparkTaskRequest] = []
    activities = _make_activities(None, 1, commits, pr_opens, linkbacks, parks, unparks)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DefectOnceDraftPr,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DefectOnceDraftPr.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                    build_id="1.4.1",
                    parked_status="Parked",
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            for _ in range(50):
                state = await handle.query(DefectOnceDraftPr.suspension_state)
                if state:
                    break
                await asyncio.sleep(0.05)

            for _ in range(50):
                if parks:
                    break
                await asyncio.sleep(0.05)

            assert len(parks) == 1
            assert parks[0].issue_key == "T-1"
            assert parks[0].parked_status == "Parked"

            await handle.signal(RESUME_SIGNAL, ResumeOn.NEWER_BUILD.value)
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert len(unparks) == 1
    assert unparks[0].issue_key == "T-1"
    assert unparks[0].target_status == "In Progress"


def _ci_input(
    poll_interval: float = 1.0,
    *,
    run_reviews: bool = False,
    run_policy: bool = False,
    run_approval: bool = False,
    run_planning: bool = False,
    run_test_triage: bool = False,
    max_repair_attempts: int = 5,
    unavailable_threshold: int = 3,
    max_cost_usd: float = 5.0,
) -> DraftPullRequestInput:
    return DraftPullRequestInput(
        task_id="T-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        base="main",
        base_sha="a" * 40,
        task="Implement the change",
        issue_key="T-1",
        file_impact=["feature.py"],
        stored_issue={"summary": "Implement the change"},
        max_repair_attempts=max_repair_attempts,
        max_cost_usd=max_cost_usd,
        run_reviews=run_reviews,
        run_policy=run_policy,
        run_approval=run_approval,
        run_planning=run_planning,
        run_test_triage=run_test_triage,
        jira_project="AGENTDEV",
        plan_approved=True,
    )


async def _start_ci_worker(env, task_queue: str, activities):
    return Worker(
        env.client,
        task_queue=task_queue,
        workflows=[
            DraftPullRequestWorkflow,
            ImplementationWorkflow,
            BranchSyncWorkflow,
            PullRequestWorkflow,
            PlanReviewWorkflow,
        ],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
        ),
        activities=activities,
    )


async def test_open_draft_pr_triggers_ci_and_advances_on_pass():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    activities = _make_activities(
        None, 1, commits, pr_opens, linkbacks, ci_triggers=ci_triggers, ci_result=SUCCESS
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.ci_status == "passed"
    assert len(ci_triggers) == 1
    assert ci_triggers[0].repo == "agentic-demo-service"
    assert ci_triggers[0].commit_sha == "c" * 40


async def test_ci_failure_repairs_and_passes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.ci_status == "passed"
    assert result.repair_attempts == 1
    assert len(repairs) == 1
    assert repairs[0].instruction == "Fix the failing assertion"
    assert len(ci_triggers) == 2
    assert len(commits) == 2
    assert commits[1].sequence == 105


async def test_code_authorship_binds_the_post_repair_commit():
    verified: list[list[dict]] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
        authorship_active=True,
        authorship_verified=verified,
        commit_shas=["c" * 40, "d" * 40],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.repair_attempts == 1
    assert verified, "merge gate verify was not reached"
    records = {r["kind"]: r for r in verified[-1]}
    assert set(records) == {"plan", "code"}
    assert records["code"]["event"]["commit"] == "d" * 40


async def test_triage_patch_verdict_runs_the_repair():
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
        triage_enabled=True,
        triage_action_value="patch",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_test_triage=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.repair_attempts == 1
    assert len(repairs) == 1


async def test_triage_flaky_verdict_reruns_without_a_repair():
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
        triage_enabled=True,
        triage_action_value="rerun",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_test_triage=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.repair_attempts == 0
    assert len(repairs) == 0
    assert len(ci_triggers) == 2


async def test_triage_environment_verdict_escalates_to_human():
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_triggers=ci_triggers,
        ci_result=FAILURE,
        repairs=repairs,
        alerts=alerts,
        triage_enabled=True,
        triage_action_value="escalate",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_test_triage=True, max_repair_attempts=2),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            result = await handle.result()

    assert result.status == CI_FAILED
    assert result.terminal_reason == "human_abandoned"
    assert len(repairs) == 0
    assert len(alerts) == 2


async def _await_human_park(handle) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if (
            state.get("reason") == SuspendReason.AWAITING_HUMAN.value
            and state.get("stage") == "repair"
        ):
            return state
        await asyncio.sleep(0.05)
    return state


async def _await_limit_park(handle, previous_key: str = "") -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(200):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        key = state.get("park_key", "")
        if state.get("reason") == SuspendReason.LIMIT_REACHED.value and key not in (
            "",
            previous_key,
        ):
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"no limit park: {state}")


async def _await_human_park_stage(handle, stage: str) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if (
            state.get("reason") == SuspendReason.AWAITING_HUMAN.value
            and state.get("stage") == stage
        ):
            return state
        await asyncio.sleep(0.05)
    return state


async def _await_ci_park(handle) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if state.get("reason") == SuspendReason.AWAITING_CI.value and state.get("stage") == "ci":
            return state
        await asyncio.sleep(0.05)
    return state


async def test_ci_repair_exhausted_parks_for_human_and_abandons():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=ci_triggers,
        ci_result=FAILURE,
        repairs=repairs,
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=2),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            assert state["resume_on"] == ResumeOn.HUMAN_APPROVAL.value
            assert state["stage"] == "repair"
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            result = await handle.result()

    assert result.status == CI_FAILED
    assert result.terminal_reason == "human_abandoned"
    assert result.repair_attempts == 2
    assert len(repairs) == 2
    assert len(ci_triggers) == 3
    assert len(alerts) == 2
    assert alerts[0].reason == "repair_exhausted"
    assert alerts[0].stage == "repair"
    assert (alerts[1].reason, alerts[1].stage, alerts[1].park_key) == (
        "human_abandoned",
        "repair",
        "",
    )
    assert [step.kind for step in alerts[1].remedy.actions] == [StepKind.RESTART_RUN]


async def test_ci_repair_stops_on_cumulative_budget():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    usage: list[UsageMetrics] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=ci_triggers,
        ci_result=FAILURE,
        repairs=repairs,
        alerts=alerts,
        usage=usage,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=10, max_cost_usd=0.35),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_limit_park(handle)
            assert state["reason"] == SuspendReason.LIMIT_REACHED.value
            assert state["stage"] == "repair"
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            result = await handle.result()

    assert result.repair_attempts == 3
    assert len(repairs) == 3
    repair_usage = [record for record in usage if record.stage == "REPAIRING"]
    assert len(repair_usage) == 3
    assert [round(record.cost_usd, 2) for record in repair_usage] == [0.1, 0.2, 0.3]
    assert len(alerts) == 2
    assert alerts[0].reason == "cost_budget_exceeded"
    assert alerts[0].stage == "repair"


async def test_repair_usage_records_carry_the_running_total():
    """Every REPAIRING write lands on one (execution_id, stage) row that the store overwrites.

    Per-attempt values therefore leave only the final attempt on record, and the stored row
    disagrees with the budget accounting that parks the run. The running total is written so the
    surviving row equals what `_budget_breach` measured.
    """
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    usage: list[UsageMetrics] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=ci_triggers,
        ci_result=FAILURE,
        repairs=repairs,
        alerts=alerts,
        usage=usage,
        repair_cache_read_tokens=3,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=10, max_cost_usd=0.35),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_limit_park(handle)
            assert state["stage"] == "repair"
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            await handle.result()

    repair_usage = [record for record in usage if record.stage == "REPAIRING"]
    assert len(repair_usage) == 3
    assert [record.llm_calls for record in repair_usage] == [1, 2, 3]
    assert [record.total_tokens for record in repair_usage] == [5, 10, 15]
    assert [record.cache_read_tokens for record in repair_usage] == [3, 6, 9]
    assert [round(record.cost_usd, 2) for record in repair_usage] == [0.1, 0.2, 0.3]


async def test_ci_repair_retry_decision_reruns_and_passes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, FAILURE, SUCCESS],
        repairs=repairs,
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=1),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park(handle)
            await handle.signal("submit_repair_decision", args=[True, "release-manager"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert result.repair_attempts == 2
    assert len(repairs) == 1
    assert len(ci_triggers) == 3
    assert len(alerts) == 1


_NEW_BASE = "d" * 40


async def _retry_once(**overrides) -> tuple[Any, dict[str, list]]:
    """Exhaust one repair, press Retry repair, and answer each later repair park with Retry."""
    recorded: dict[str, list] = {
        "commits": [],
        "resyncs": [],
        "dependencies": [],
        "alerts": [],
    }
    decisions = overrides.pop("decisions", [True])
    activities = _make_activities(
        None,
        1,
        recorded["commits"],
        [],
        [],
        ci_results=[FAILURE, FAILURE, SUCCESS],
        repairs=[],
        alerts=recorded["alerts"],
        resync_requests=recorded["resyncs"],
        dependency_requests=recorded["dependencies"],
        **overrides,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=1),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            answered: set[str] = set()
            for retry in decisions:
                park_key = ""
                while not park_key or park_key in answered:
                    state = await handle.query(DraftPullRequestWorkflow.suspension_state)
                    if (
                        state.get("reason") == SuspendReason.AWAITING_HUMAN.value
                        and state.get("stage") == "repair"
                    ):
                        park_key = state.get("park_key", "")
                    await asyncio.sleep(0.05)
                answered.add(park_key)
                await handle.signal("submit_repair_decision", args=[retry, "lead"])
            result = await handle.result()
    return result, recorded


async def test_a_retry_after_the_base_moved_commits_the_branch_onto_it_before_the_checks():
    result, recorded = await _retry_once(
        resync_results=[ResyncResult(status="merged", base_head=_NEW_BASE)]
    )

    assert result.status == PR_PROMOTED
    resync = recorded["commits"][2]
    assert (resync.commit, resync.sequence) == (_NEW_BASE, 107)
    assert recorded["resyncs"][0].commit == "c" * 40
    assert recorded["dependencies"][2].base_ref == _NEW_BASE


async def test_a_run_moved_onto_a_newer_build_gates_the_commits_it_makes_there(monkeypatch):
    from temporalio.worker import UnsandboxedWorkflowRunner

    commits: list[CommitPushRequest] = []
    dependencies: list[DependencyStepRequest] = []
    resyncs: list = []
    activities = _make_activities(
        None,
        1,
        commits,
        [],
        [],
        ci_results=[FAILURE, FAILURE, SUCCESS],
        repairs=[],
        resync_requests=resyncs,
        dependency_requests=dependencies,
        resync_results=[ResyncResult(status="merged", base_head=_NEW_BASE)],
    )
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]
    patched = workflow.patched
    new_patches = ("commit-gates-", "retry-resync-")

    def worker(env, task_queue):
        return Worker(
            env.client,
            task_queue=task_queue,
            workflows=[DraftPullRequestWorkflow, *children],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
            max_cached_workflows=0,
        )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as old_build:
            old_build.setattr(
                workflow,
                "patched",
                lambda name: not name.startswith(new_patches) and patched(name),
            )
            async with worker(env, task_queue):
                handle = await env.client.start_workflow(
                    DraftPullRequestWorkflow.run,
                    _ci_input(max_repair_attempts=1),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                await _await_human_park(handle)
        async with worker(env, task_queue):
            await handle.signal("submit_repair_decision", args=[True, "lead"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert [c.guard_workflow_files for c in commits] == [False, False, True]
    assert [d.commit for d in dependencies] == [_NEW_BASE]
    assert len(resyncs) == 1


async def test_a_retry_with_an_unchanged_base_commits_nothing_new():
    result, recorded = await _retry_once()

    assert result.status == PR_PROMOTED
    assert len(recorded["commits"]) == 2
    assert len(recorded["resyncs"]) == 1


async def test_a_re_sync_conflict_parks_for_retry_and_retry_syncs_again():
    conflict = ResyncResult(status="conflicts", base_head=_NEW_BASE, conflicts=["shared.py"])
    merged = ResyncResult(status="merged", base_head=_NEW_BASE)
    result, recorded = await _retry_once(resync_results=[conflict, merged], decisions=[True, True])

    assert result.status == PR_PROMOTED
    assert len(recorded["resyncs"]) == 2
    parked = [a for a in recorded["alerts"] if a.stage == "repair"]
    assert "shared.py" in parked[1].detail
    assert {step.kind for step in parked[1].remedy.actions} == {
        StepKind.RETRY_REPAIR,
        StepKind.ABANDON_REPAIR,
    }


async def test_abandoning_a_re_sync_conflict_ends_the_run_without_a_commit():
    conflict = ResyncResult(status="conflicts", base_head=_NEW_BASE, conflicts=["shared.py"])
    result, recorded = await _retry_once(resync_results=[conflict], decisions=[True, False])

    assert result.terminal_reason == HUMAN_ABANDONED
    assert len(recorded["commits"]) == 2


async def test_ci_failure_unactionable_parks_for_human():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=ci_triggers,
        ci_result=FAILURE,
        repairs=repairs,
        repair_actionable=False,
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park(handle)
            await handle.signal("submit_repair_decision", args=[False, ""])
            result = await handle.result()

    assert result.status == CI_FAILED
    assert result.terminal_reason == "human_abandoned"
    assert result.ci_status == "failed"
    assert result.repair_attempts == 0
    assert len(repairs) == 0
    assert len(ci_triggers) == 1
    assert len(alerts) == 2


async def test_ci_pass_flows_through_review_gates_with_attestations():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    review_requests: list[GateContext] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        review_requests=review_requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.review_status == "passed"
    assert result.code_verdict == "pass"
    assert result.security_verdict == "pass"
    assert result.acceptance_verdict == "pass"
    assert result.review_attestations == {
        "code-review": "sha256:code-review",
        "security-review": "sha256:security-review",
        "acceptance": "sha256:acceptance",
    }
    assert len(review_requests) == 3
    assert review_requests[0].ref == "c" * 40
    assert review_requests[0].repo_root == "/tmp/ws/agent-T-1"


async def test_review_gates_run_on_the_review_queue_the_intake_named():
    from dataclasses import replace

    review_queue = "agent-platform-review-beta"
    reviewed: list[tuple[str, str]] = []

    @activity.defn(name="evaluate_gate")
    async def review_worker_evaluate_gate(req: EvaluateGateRequest) -> GateResult:
        reviewed.append((req.gate, activity.info().task_queue))
        return GateResult(
            name=req.gate,
            kind="review",
            verdict="pass",
            attestation_hash=f"sha256:{req.gate}",
            summary="reviewed",
            metadata={"model_id": "review-model", "prompt_version": f"{req.gate}/v1"},
        )

    activities = _make_activities(None, 1, [], [], [], ci_result=SUCCESS)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        review_worker = Worker(
            env.client, task_queue=review_queue, activities=[review_worker_evaluate_gate]
        )
        async with await _start_ci_worker(env, task_queue, activities), review_worker:
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                replace(_ci_input(run_reviews=True), review_task_queue=review_queue),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.review_status == "passed"
    assert sorted(reviewed) == [
        ("acceptance", review_queue),
        ("code-review", review_queue),
        ("security-review", review_queue),
    ]


async def test_review_blocking_finding_stops_before_policy_gate():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    review_requests: list[GateContext] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        review_requests=review_requests,
        security_verdict="block",
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == REVIEW_BLOCKED
    assert result.review_status == "blocked"
    assert result.code_verdict == "pass"
    assert result.security_verdict == "block"
    assert result.acceptance_verdict == "pass"
    assert result.review_attestations == {
        "code-review": "sha256:code-review",
        "security-review": "sha256:security-review",
        "acceptance": "sha256:acceptance",
    }
    assert result.review_findings == {
        "code-review": [],
        "security-review": ["security-review blocking finding"],
        "acceptance": [],
    }
    assert result.review_summaries == {
        "code-review": "reviewed",
        "security-review": "reviewed",
        "acceptance": "reviewed",
    }
    assert len(review_requests) == 3
    assert len(alerts) == 1
    assert alerts[0].reason == REVIEW_BLOCKED
    assert alerts[0].stage == "review"


async def test_review_pause_for_human_parks_then_approve_proceeds():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park_stage(handle, "review")
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            assert state["resume_on"] == ResumeOn.HUMAN_APPROVAL.value
            await handle.signal(
                "submit_gate_decision", args=["security-review", True, "security-lead"]
            )
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert result.review_status == "passed"
    assert alerts[0].reason == "gate_paused:security-review"
    assert alerts[0].stage == "review"


async def test_review_pause_for_human_reject_blocks():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "review")
            await handle.signal(
                "submit_gate_decision", args=["security-review", False, "security-lead"]
            )
            result = await handle.result()

    assert result.status == REVIEW_BLOCKED
    assert result.review_status == "blocked"
    assert any(alert.reason == "gate_paused:security-review" for alert in alerts)
    assert any(alert.reason == REVIEW_BLOCKED for alert in alerts)


async def test_fail_closed_gate_still_blocks_beside_a_pause_gate():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        code_verdict="block",
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == REVIEW_BLOCKED
    assert len(alerts) == 1
    assert alerts[0].reason == REVIEW_BLOCKED


async def test_policy_human_review_parks_then_approve_promotes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        policy_allow=False,
        policy_human_review=["gate_failed:security-review"],
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park_stage(handle, "policy")
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            await handle.signal(
                "submit_gate_decision",
                args=["gate_failed:security-review", True, "release-manager"],
            )
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert result.policy_status == "allowed"


async def test_policy_human_review_reject_denies():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        policy_allow=False,
        policy_human_review=["gate_failed:security-review"],
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "policy")
            await handle.signal(
                "submit_gate_decision",
                args=["gate_failed:security-review", False, "release-manager"],
            )
            result = await handle.result()

    assert result.status == POLICY_DENIED
    assert result.policy_status == "denied"
    assert any(alert.reason == POLICY_DENIED for alert in alerts)


async def test_plan_human_gate_parks_then_approve_continues():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    plan_evidence: list[PlanEvidenceRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        plan_pause=True,
        plan_evidence=plan_evidence,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park_stage(handle, "plan")
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            await handle.signal("submit_gate_decision", args=["plan", True, "tech-lead"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert len(plan_evidence) == 1
    assert plan_evidence[0].plan
    assert plan_evidence[0].verdict == "pass"


async def test_plan_human_gate_reject_stops_the_run():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    plan_evidence: list[PlanEvidenceRequest] = []
    persisted: list[ExecutionState] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        plan_pause=True,
        plan_evidence=plan_evidence,
        persisted_states=persisted,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "plan")
            await handle.signal("submit_gate_decision", args=["plan", False, "tech-lead"])
            result = await handle.result()

    assert result.status == PLAN_REJECTED
    assert result.plan_status == "rejected"
    assert plan_evidence == []
    assert persisted[-1].state == "CANCELLED"


async def test_plan_human_gate_is_inert_by_default():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        plan_pause=False,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED


async def test_plan_stage_feeds_the_reviewed_plan_to_the_coder():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    impl_requests: list[ImplementationRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        impl_requests=impl_requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert len(impl_requests) == 1
    assert "Edit feature.py" in impl_requests[0].plan
    assert impl_requests[0].context_package == {"nodes": ["feature.py"]}


@pytest.mark.parametrize("run_planning", [False, True])
@pytest.mark.parametrize(
    "description",
    ["", "\nKeep each record's original order.\nPreserve Unicode values such as café.\n"],
)
async def test_original_ticket_description_reaches_coding_with_or_without_a_plan(
    run_planning, description
):
    impl_requests: list[ImplementationRequest] = []
    activities = _make_activities(None, 1, [], [], [], impl_requests=impl_requests)
    params = _ci_input(run_reviews=True, run_planning=run_planning)
    params.task = "Add CSV export"
    params.task_description = description

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                params,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert len(impl_requests) == 1
    expected_task = f"{params.task}\n\n{description}" if description else params.task
    assert impl_requests[0].task == expected_task
    assert bool(impl_requests[0].plan) is run_planning


async def test_a_title_only_implementation_history_replays_without_adding_the_description(
    monkeypatch,
):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    patch_id = "implementation-task-description"
    impl_requests: list[ImplementationRequest] = []
    activities = _make_activities(None, 1, [], [], [], impl_requests=impl_requests)
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]
    params = _ci_input(run_reviews=True, run_planning=True)
    params.task_description = "Keep each record's original order."
    patched = workflow.patched

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as pre_patch:
            pre_patch.setattr(
                workflow,
                "patched",
                lambda name: name != patch_id and patched(name),
            )
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[DraftPullRequestWorkflow, *children],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=activities,
            ):
                handle = await env.client.start_workflow(
                    DraftPullRequestWorkflow.run,
                    params,
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                await handle.result()
                history = await handle.fetch_history()

    assert [request.task for request in impl_requests] == [params.task]
    assert not any(patch_id in marker for marker in _patch_markers(history))
    replayed_tasks: list[str] = []
    execute_child_workflow = workflow.execute_child_workflow

    async def capture_implementation_task(child, arg, **kwargs):
        if child == ImplementationWorkflow.run:
            replayed_tasks.append(arg.task)
        return await execute_child_workflow(child, arg, **kwargs)

    with monkeypatch.context() as replay:
        replay.setattr(workflow, "execute_child_workflow", capture_implementation_task)
        replayed = await Replayer(
            workflows=[DraftPullRequestWorkflow, *children],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ).replay_workflow(history, raise_on_replay_failure=False)

    assert replayed.replay_failure is None
    assert replayed_tasks == [params.task]


async def test_plan_rejected_stops_before_implementation():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    impl_requests: list[ImplementationRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    persisted: list[ExecutionState] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        plan_verdict="block",
        impl_requests=impl_requests,
        alerts=alerts,
        persisted_states=persisted,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PLAN_REJECTED
    assert result.plan_status == "rejected"
    assert result.plan_verdict == "block"
    assert impl_requests == []
    assert len(alerts) == 1
    assert alerts[0].reason == PLAN_REJECTED
    assert alerts[0].stage == "plan"
    assert persisted[-1].state == "CANCELLED"


async def test_planning_and_coding_pin_one_ris_commit():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    context_requests: list[ContextRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        context_requests=context_requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert len(context_requests) == 1
    assert context_requests[0].commit == "a" * 40


async def test_agent_summary_reaches_pr_and_review_subject():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    review_requests: list[GateContext] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        review_requests=review_requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert "done" in pr_opens[0].body
    assert "Automated plan and code review passed." in pr_opens[0].body
    assert any(
        "Implementation summary:" in request.subject and "done" in request.subject
        for request in review_requests
    )


async def test_ci_pass_reaches_the_policy_gate_and_promotes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    scan_requests: list[ScanChangeRequest] = []
    policy_requests: list[EvaluatePromotionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        scan_requests=scan_requests,
        policy_requests=policy_requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert result.policy_status == "allowed"
    assert result.policy_bundle_version == "0.4.0"
    assert result.policy_next_action == "proceed"
    assert result.policy_reasons == []
    assert result.scan_blocking_findings == 0
    assert len(scan_requests) == 1
    assert scan_requests[0].ref == "c" * 40
    assert scan_requests[0].repo_root == "/tmp/ws/agent-T-1"
    assert len(policy_requests) == 1
    assert policy_requests[0].jira_project == "AGENTDEV"
    assert policy_requests[0].ci_status == "success"
    assert policy_requests[0].pr_authorized is True
    assert {gate.name for gate in policy_requests[0].gates} == {
        "code-review",
        "security-review",
        "acceptance",
        "semgrep",
        "trivy",
    }
    assert all(gate.verdict == "pass" for gate in policy_requests[0].gates)
    assert set(policy_requests[0].required_gates) == {
        "code-review",
        "security-review",
        "acceptance",
        "semgrep",
        "trivy",
    }


async def test_run_emits_the_determinism_manifest_first():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    manifest_requests: list[RecordManifestRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        manifest_requests=manifest_requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert len(manifest_requests) == 1
    assert manifest_requests[0].execution_id == "exec-1"
    assert manifest_requests[0].task_id == "T-1"
    assert manifest_requests[0].repository_sha == "a" * 40


async def test_policy_deny_on_blocking_finding_blocks_promotion():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    policy_requests: list[EvaluatePromotionRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        scan_blocking_findings=1,
        policy_requests=policy_requests,
        policy_allow=False,
        policy_reasons=["gate_failed:semgrep"],
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == POLICY_DENIED
    assert result.policy_status == "denied"
    assert result.policy_next_action == "block_and_report"
    assert result.policy_reasons == ["gate_failed:semgrep"]
    assert result.scan_blocking_findings == 1
    assert "semgrep" in {gate.name for gate in policy_requests[0].gates}
    assert len(alerts) == 1
    assert alerts[0].reason == POLICY_DENIED
    assert alerts[0].stage == "policy"


async def test_scan_findings_and_severity_reach_policy_and_result():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    scan_requests: list[ScanChangeRequest] = []
    policy_requests: list[EvaluatePromotionRequest] = []
    findings = [
        ScanFindingRecord(
            finding_id="f1",
            scanner="semgrep",
            kind="sast",
            severity="high",
            title="SQL injection",
            detail="tainted input",
            location="app/db.py:20",
            rule_id="python.sql-injection",
        ),
        ScanFindingRecord(
            finding_id="f2",
            scanner="trivy",
            kind="dependency",
            severity="medium",
            title="Vulnerable dependency",
            location="requirements.txt",
            rule_id="CVE-2024-0001",
        ),
    ]
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        scan_blocking_findings=1,
        scan_result_findings=findings,
        scan_result_severity_counts={"high": 1, "medium": 1},
        scan_requests=scan_requests,
        policy_requests=policy_requests,
        policy_allow=False,
        policy_reasons=["gate_failed:semgrep"],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.scan_blocking_findings == 1
    assert result.scan_severity_counts == {"high": 1, "medium": 1}
    assert [finding["rule_id"] for finding in result.scan_findings] == [
        "python.sql-injection",
        "CVE-2024-0001",
    ]
    assert [finding["severity"] for finding in result.scan_findings] == ["high", "medium"]
    scanner_gates = {
        gate.name: gate.verdict
        for gate in policy_requests[0].gates
        if gate.name in {"semgrep", "trivy"}
    }
    assert scanner_gates == {"semgrep": "block", "trivy": "pass"}
    assert policy_requests[0].severity_counts == {"high": 1, "medium": 1}
    assert [finding["rule_id"] for finding in policy_requests[0].scan_findings] == [
        "python.sql-injection",
        "CVE-2024-0001",
    ]


async def test_scan_backend_unavailable_fails_closed_after_retries():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    scan_requests: list[ScanChangeRequest] = []
    policy_requests: list[EvaluatePromotionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        scan_change_raises=True,
        scan_requests=scan_requests,
        policy_requests=policy_requests,
        policy_allow=False,
        policy_reasons=["gate_failed:semgrep"],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # The activity was retried to exhaustion, then the workflow failed the scanner gates closed
    # rather than crashing, and no pull request was opened.
    assert len(scan_requests) == 3
    assert result.status != PR_PROMOTED
    assert result.pr_number == 0
    assert [f["rule_id"] for f in result.scan_findings] == [
        "gate-backend-error",
        "gate-backend-error",
    ]
    assert result.scan_blocking_findings == 2
    scanner_gates = {
        gate.name: gate.verdict
        for gate in policy_requests[0].gates
        if gate.name in {"semgrep", "trivy"}
    }
    assert scanner_gates == {"semgrep": "block", "trivy": "block"}


async def _await_approval_park(handle) -> dict[str, str]:
    state: dict[str, str] = {}
    for _ in range(50):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if (
            state.get("reason") == SuspendReason.AWAITING_REVIEW.value
            and state.get("stage") == "approval"
        ):
            return state
        await asyncio.sleep(0.05)
    return state


async def test_approval_parks_and_promotes_pr_on_approve():
    approvals: list[RecordApprovalRequest] = []
    promotions: list[PromotePrRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        approvals=approvals,
        promotions=promotions,
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True, run_approval=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            state = await _await_approval_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_REVIEW.value
            assert state["resume_on"] == ResumeOn.HUMAN_APPROVAL.value
            assert state["stage"] == "approval"

            await handle.signal("submit_pr_approval", args=[True, "release-manager"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert result.approval_status == "approved"
    assert result.approver == "release-manager"
    assert result.promoted is True
    assert len(approvals) == 1
    assert approvals[0].approved is True
    assert approvals[0].sequence == 115
    assert len(promotions) == 1
    assert promotions[0].sequence == 116
    assert "## Changes" in promotions[0].summary
    assert alerts == []


async def test_approval_rejection_cancels_promotion():
    approvals: list[RecordApprovalRequest] = []
    promotions: list[PromotePrRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        approvals=approvals,
        promotions=promotions,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True, run_approval=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            await _await_approval_park(handle)
            await handle.signal("submit_pr_approval", args=[False, "release-manager"])
            result = await handle.result()

    assert result.status == PROMOTION_CANCELLED
    assert result.approval_status == "rejected"
    assert result.terminal_reason == "approval_rejected"
    assert result.promoted is False
    assert len(approvals) == 1
    assert approvals[0].approved is False
    assert len(promotions) == 0


async def test_approval_snapshot_drift_cancels_promotion():
    approvals: list[RecordApprovalRequest] = []
    promotions: list[PromotePrRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        approvals=approvals,
        promotions=promotions,
        alerts=alerts,
        recheck_requires_pause=True,
        recheck_category="scope_change",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True, run_approval=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            await _await_approval_park(handle)
            await handle.signal("submit_pr_approval", args=[True, "release-manager"])
            result = await handle.result()

    assert result.status == PROMOTION_CANCELLED
    assert result.approval_status == "approved"
    assert result.terminal_reason == "scope_change"
    assert result.promoted is False
    assert len(approvals) == 1
    assert len(promotions) == 0
    assert alerts[-1].detail == "issue changed"


async def test_authorship_signs_both_and_code_binds_the_promoted_commit():
    verified: list[list[dict]] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        authorship_active=True,
        authorship_verified=verified,
        commit_shas=["c" * 40],
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                DraftPullRequestInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    task="Implement the change",
                    issue_key="T-1",
                    file_impact=["feature.py"],
                    stored_issue={"summary": "Implement the change"},
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert verified, "merge gate verify was not reached"
    records = {r["kind"]: r for r in verified[-1]}
    assert set(records) == {"plan", "code"}
    assert records["code"]["event"]["commit"] == "c" * 40


def test_sequence_stride_exceeds_the_worst_case_intra_execution_offset():
    max_repair_attempts = DraftPullRequestInput.max_repair_attempts
    worst_case_offset = _CI_TRIGGER_OFFSET + _REPAIR_STRIDE * max_repair_attempts + _PROMOTE_OFFSET
    assert worst_case_offset < _SEQUENCE_STRIDE


async def test_every_sequenced_action_clears_the_gate_when_ci_needed_a_repair():
    """The gate admits only strictly increasing sequences, so the numbers the workflow
    hands out have to rise in the order the stages actually run. A repair commits before
    the pull request opens, so its sequence has to stay below the one that opens the PR."""
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    approvals: list[RecordApprovalRequest] = []
    promotions: list[PromotePrRequest] = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        [],
        ci_triggers=[],
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
        approvals=approvals,
        promotions=promotions,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert len(repairs) == 1
    assert len(commits) == 2
    assert len(pr_opens) == 1

    issued = [
        ("github.commit", commits[0].sequence),
        ("github.commit", commits[1].sequence),
        ("github.open_pr", pr_opens[0].sequence),
        *[("github.promotion_approval", a.sequence) for a in approvals],
        *[("github.mark_ready", p.sequence) for p in promotions],
    ]

    gate = InMemorySequenceGate()
    discarded = []
    for operation, sequence in issued:
        decision = await gate.admit(SequencedAction("T-1", "exec-1", sequence, operation=operation))
        if decision.outcome != ADMITTED:
            discarded.append((operation, sequence, decision.current_sequence))

    assert discarded == []


async def test_authorship_verification_failure_blocks_promotion():
    from temporalio.client import WorkflowFailureError

    promotions: list[PromotePrRequest] = []
    activities = _make_activities(
        None, 1, [], [], [], promotions=promotions, authorship_verify_raises=True
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[
                DraftPullRequestWorkflow,
                ImplementationWorkflow,
                BranchSyncWorkflow,
                PullRequestWorkflow,
                PlanReviewWorkflow,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=activities,
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    DraftPullRequestWorkflow.run,
                    DraftPullRequestInput(
                        task_id="T-1",
                        execution_id="exec-1",
                        repo="agentic-demo-service",
                        base="main",
                        base_sha="a" * 40,
                        task="Implement the change",
                        issue_key="T-1",
                        file_impact=["feature.py"],
                        stored_issue={"summary": "Implement the change"},
                    ),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    assert len(promotions) == 0


def _input(**kwargs) -> DraftPullRequestInput:
    base = {
        "task_id": "6948",
        "execution_id": "intake-1",
        "repo": "example-service",
        "base": "main",
        "base_sha": "abc123",
        "task": "Remove the size prop",
    }
    return DraftPullRequestInput(**{**base, **kwargs})


def test_acceptance_criteria_prefer_the_dedicated_stored_issue_field():
    from services.orchestration.temporal_workflows.draft_pull_request import _acceptance_criteria

    params = _input(
        stored_issue={"acceptance_criteria": "The size prop is removed"},
        task_description="A long description that should not be used",
    )

    assert _acceptance_criteria(params) == "The size prop is removed"


def test_acceptance_criteria_fall_back_to_the_description_when_the_field_is_empty():
    """The rebar backend has no criteria field and states them inside the description."""
    from services.orchestration.temporal_workflows.draft_pull_request import _acceptance_criteria

    description = "## Acceptance Criteria\n* The size prop is removed\n* The snapshot is updated"
    params = _input(stored_issue={"acceptance_criteria": ""}, task_description=description)

    assert _acceptance_criteria(params) == description


def test_acceptance_criteria_are_blank_when_the_ticket_states_none():
    from services.orchestration.temporal_workflows.draft_pull_request import _acceptance_criteria

    assert _acceptance_criteria(_input()) == ""


async def test_repository_pull_request_template_shapes_the_pr_body():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    template = (
        "## Ticket\n\nhttps://tracker.example/browse/{TICKET ID}\n\n"
        "## Changes\n\n(What was added, updated, or removed in this PR.)\n\n"
        "## Testing\n\n(Provide evidence that the code works as expected.)\n\n"
        "- [ ] Deploy a preview\n"
    )
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        pr_template=template,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    body = pr_opens[0].body
    assert body.count("## ") == 3
    assert "(What was added, updated, or removed in this PR.)" not in body
    assert "- [ ] Deploy a preview" in body
    assert "https://tracker.example/browse/T-1" in body


@pytest.mark.asyncio
async def test_a_declared_tracker_key_names_the_pull_request():
    from dataclasses import replace

    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        pr_template="## Ticket\n\nhttps://tracker.example/browse/{TICKET ID}\n",
    )
    params = replace(
        _ci_input(),
        stored_issue={"summary": "Implement the change", "labels": ["adp", "jira-proj-33739"]},
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                params,
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert pr_opens[0].title == "PROJ-33739: Implement the change"
    assert "https://tracker.example/browse/PROJ-33739" in pr_opens[0].body
    assert linkbacks[0].issue_key == "T-1"


async def test_every_commit_carries_the_declared_file_impact():
    """Staging honours the declaration only if both commit stages actually pass it through."""
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_triggers=[],
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert len(commits) == 2
    assert [c.file_impact for c in commits] == [["feature.py"], ["feature.py"]]


async def test_checks_changed_paths_carry_the_ticket_file_impact():
    ci_triggers: list = []
    activities = _make_activities(None, 1, [], [], [], ci_triggers=ci_triggers)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert ci_triggers, "the checks activity must run"
    assert tuple(ci_triggers[0].changed_paths) == ("feature.py",)


async def test_repair_changed_paths_carry_the_ticket_file_impact():
    ci_triggers: list = []
    repairs: list[ApplyCIRepairRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        repairs=repairs,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert repairs, "the repair activity must run"
    assert tuple(repairs[0].changed_paths) == ("feature.py",)


def _patch_markers(history) -> set[str]:
    markers: set[str] = set()
    for event in history.events:
        if not event.HasField("marker_recorded_event_attributes"):
            continue
        for payloads in event.marker_recorded_event_attributes.details.values():
            for payload in payloads.payloads:
                markers.add(payload.data.decode())
    return markers


async def test_the_run_model_key_is_deleted_once_when_the_run_ends():
    deletions: list[DeleteRunModelKeyRequest] = []
    activities = _make_activities(
        None, 1, [], [], [], ci_result=SUCCESS, model_key_deletions=deletions
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=workflow_id,
                task_queue=task_queue,
            )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert [req.execution_id for req in deletions] == ["exec-1"]
    assert any("delete-run-model-key" in marker for marker in _patch_markers(history))


async def test_a_gate_engine_budget_stop_fails_the_run_as_a_cap_breach():
    from services.integration.litellm_admin import TENANT_MODEL_BUDGET_EXCEEDED

    alerts: list[AlertHumanInterventionRequest] = []
    policy_requests: list[EvaluatePromotionRequest] = []
    persisted: list[ExecutionState] = []
    audits: list[AuditEvent] = []
    deletions: list[DeleteRunModelKeyRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        alerts=alerts,
        policy_requests=policy_requests,
        scan_budget_stop=TENANT_MODEL_BUDGET_EXCEEDED,
        persisted_states=persisted,
        audit_events=audits,
        model_key_deletions=deletions,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == POLICY_DENIED
    assert result.terminal_reason == TENANT_MODEL_BUDGET_EXCEEDED
    assert result.policy_reasons == [TENANT_MODEL_BUDGET_EXCEEDED]
    assert policy_requests == []
    assert persisted[-1].state == "FAILED"
    stops = [event for event in audits if event.event_type == "MODEL_BUDGET_EXCEEDED"]
    assert [(event.decision, event.reason) for event in stops] == [
        ("cap_breach", TENANT_MODEL_BUDGET_EXCEEDED)
    ]
    assert [(alert.reason, alert.stage) for alert in alerts] == [
        (TENANT_MODEL_BUDGET_EXCEEDED, "policy")
    ]
    assert len(deletions) == 1


@workflow.defn(name="DraftPullRequestWorkflow")
class _PrePatchDraftPr(DraftPullRequestWorkflow):
    """The workflow as it ran before the run model key was deleted at the end of a run."""

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        return await super().run(params)

    async def _delete_run_model_key(self, params: DraftPullRequestInput) -> None:
        return None


async def test_a_run_started_before_the_key_deletion_replays_without_it():
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    deletions: list[DeleteRunModelKeyRequest] = []
    activities = _make_activities(
        None, 1, [], [], [], ci_result=SUCCESS, model_key_deletions=deletions
    )
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_PrePatchDraftPr, *children],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
        ):
            await env.client.execute_workflow(
                "DraftPullRequestWorkflow",
                _ci_input(),
                id=workflow_id,
                task_queue=task_queue,
                result_type=DraftPullRequestOutput,
            )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert deletions == []
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *children],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_a_repair_refused_for_the_run_budget_alerts_with_the_budget_reason():
    from services.integration.litellm_admin import RUN_MODEL_BUDGET_EXCEEDED

    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=FAILURE,
        repairs=repairs,
        alerts=alerts,
        repair_terminal_reason=RUN_MODEL_BUDGET_EXCEEDED,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=3),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park(handle)
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            result = await handle.result()

    assert len(repairs) == 1
    assert [(alert.reason, alert.stage) for alert in alerts] == [
        (RUN_MODEL_BUDGET_EXCEEDED, "repair"),
        ("human_abandoned", "repair"),
    ]
    assert result.terminal_reason == "human_abandoned"


def _parking_activities(
    deletions: list[DeleteRunModelKeyRequest], finalizations: list[FinalizeClaimRequest]
):
    return _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=FAILURE,
        triage_enabled=True,
        triage_action_value="escalate",
        model_key_deletions=deletions,
        claim_finalizations=finalizations,
    )


async def _cancel_parked_run(env, handle) -> None:
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import CancelledError

    state = await _await_human_park(handle)
    assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
    await handle.cancel()
    with pytest.raises(WorkflowFailureError) as failed:
        await handle.result()
    assert isinstance(failed.value.cause, CancelledError)


async def test_a_run_cancelled_while_parked_settles_its_claim_and_deletes_its_key():
    from temporalio.client import WorkflowExecutionStatus

    deletions: list[DeleteRunModelKeyRequest] = []
    finalizations: list[FinalizeClaimRequest] = []
    activities = _parking_activities(deletions, finalizations)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_test_triage=True, max_repair_attempts=2),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _cancel_parked_run(env, handle)
            description = await handle.describe()
            history = await handle.fetch_history()

    assert description.status == WorkflowExecutionStatus.CANCELED
    assert [req.execution_id for req in deletions] == ["exec-1"]
    assert [req.completed for req in finalizations] == [False]
    assert any("finalize-on-cancel" in marker for marker in _patch_markers(history))


@workflow.defn(name="DraftPullRequestWorkflow")
class _PreCancelFinalizeDraftPr(DraftPullRequestWorkflow):
    """The workflow as it ran before a cancelled run settled its claim."""

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        try:
            outcome = await self._orchestrate(params)
        except Exception:
            await self._finalize_claim(params)
            raise
        await self._finalize_claim(params)
        return outcome


async def test_a_run_cancelled_before_the_cancel_cleanup_replays_without_it():
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    deletions: list[DeleteRunModelKeyRequest] = []
    finalizations: list[FinalizeClaimRequest] = []
    activities = _parking_activities(deletions, finalizations)
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_PreCancelFinalizeDraftPr, *children],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                "DraftPullRequestWorkflow",
                _ci_input(run_test_triage=True, max_repair_attempts=2),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
                result_type=DraftPullRequestOutput,
            )
            await _cancel_parked_run(env, handle)
            history = await handle.fetch_history()

    assert deletions == []
    assert finalizations == []
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *children],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


@workflow.defn(name="DefectOnceDraftPr")
class _PreAlertDefectOnceDraftPr(DefectOnceDraftPr):
    """The code-defect park as it ran before it alerted, with no alert on the push_branch stage."""

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        return await super().run(params)

    async def _terminal_alert(self, params, workflow_id, *, stage, **kwargs) -> str:
        if stage == "push_branch":
            return ""
        return await super()._terminal_alert(params, workflow_id, stage=stage, **kwargs)


async def test_a_run_parked_on_a_code_defect_before_the_alert_replays_without_it(monkeypatch):
    """Before the code-defect park alerted, its path recorded no patch marker and ran no alert
    activity. The recording answers False for the patch without recording its marker, and its
    workflow skips the alert, so the history matches a pre-patch run whatever the guard does."""
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(None, 1, [], [], [], alerts=alerts)
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow]
    patched = workflow.patched

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as pre_patch:
            pre_patch.setattr(
                workflow,
                "patched",
                lambda patch_id: patch_id != "code-defect-alert" and patched(patch_id),
            )
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[_PreAlertDefectOnceDraftPr, *children],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=activities,
            ):
                handle = await env.client.start_workflow(
                    "DefectOnceDraftPr",
                    DraftPullRequestInput(
                        task_id="T-1",
                        execution_id="exec-1",
                        repo="agentic-demo-service",
                        base="main",
                        base_sha="a" * 40,
                        task="Implement the change",
                        issue_key="T-1",
                        file_impact=["feature.py"],
                        stored_issue={"summary": "Implement the change"},
                        build_id="1.4.1",
                    ),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                    result_type=DraftPullRequestOutput,
                )
                state: dict[str, str] = {}
                for _ in range(50):
                    state = await handle.query(DefectOnceDraftPr.suspension_state)
                    if state:
                        break
                    await asyncio.sleep(0.05)
                assert state["reason"] == SuspendReason.AWAITING_FIX.value
                await handle.signal(RESUME_SIGNAL, ResumeOn.NEWER_BUILD.value)
                await handle.result()
                history = await handle.fetch_history()

    assert alerts == []
    assert not any("code-defect-alert" in marker for marker in _patch_markers(history))
    replayed = await Replayer(
        workflows=[DefectOnceDraftPr, *children], workflow_runner=UnsandboxedWorkflowRunner()
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


_CHILD_WORKFLOWS = [
    ImplementationWorkflow,
    BranchSyncWorkflow,
    PullRequestWorkflow,
    PlanReviewWorkflow,
]


@workflow.defn(name="DraftPullRequestWorkflow")
class _PreParkKeyDraftPr(DraftPullRequestWorkflow):
    """The workflow as it ran before parks carried a decision key."""

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        return await super().run(params)

    def _park_keys_enabled(self) -> bool:
        return False


def _unsandboxed_worker(env, task_queue: str, activities, workflow_class):
    from temporalio.worker import UnsandboxedWorkflowRunner

    return Worker(
        env.client,
        task_queue=task_queue,
        workflows=[workflow_class, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
        activities=activities,
        max_cached_workflows=0,
    )


async def _await_park_key(handle, stage: str) -> str:
    state: dict = {}
    for _ in range(100):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        if (
            state.get("reason") == SuspendReason.AWAITING_HUMAN.value
            and state.get("stage") == stage
            and state.get("park_key")
        ):
            return state["park_key"]
        await asyncio.sleep(0.05)
    raise AssertionError(f"no keyed park at {stage}: {state}")


def _decisions(audits: list[AuditEvent]) -> list[AuditEvent]:
    return [event for event in audits if event.event_type == "HUMAN_INTERVENTION_DECISION"]


@pytest.mark.parametrize(
    ("stage", "options", "workflow_input", "signal", "unkeyed_subject"),
    [
        (
            "review",
            {
                "ci_result": SUCCESS,
                "security_verdict": "block",
                "review_on_block": {"security-review": "pause_for_human"},
            },
            {"run_reviews": True},
            ("submit_gate_decision", ["security-review", True, "security-lead"]),
            "review:security-review",
        ),
        (
            "repair",
            {"ci_results": [FAILURE, FAILURE, SUCCESS]},
            {"max_repair_attempts": 1},
            ("submit_repair_decision", [True, "release-manager"]),
            "1",
        ),
    ],
)
async def test_a_park_recorded_before_park_keys_continues_on_the_keyed_worker(
    stage, options, workflow_input, signal, unkeyed_subject
):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    alerts: list[AlertHumanInterventionRequest] = []
    audits: list[AuditEvent] = []
    activities = _make_activities(
        None, 1, [], [], [], alerts=alerts, audit_events=audits, **options
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with _unsandboxed_worker(env, task_queue, activities, _PreParkKeyDraftPr):
            handle = await env.client.start_workflow(
                "DraftPullRequestWorkflow",
                _ci_input(**workflow_input),
                id=workflow_id,
                task_queue=task_queue,
                result_type=DraftPullRequestOutput,
            )
            await _await_human_park_stage(handle, stage)
        async with _unsandboxed_worker(env, task_queue, activities, DraftPullRequestWorkflow):
            await handle.signal(signal[0], args=signal[1])
            result = await handle.result()
            history = await handle.fetch_history()

    assert result.status == PR_PROMOTED
    assert [alert.park_key for alert in alerts] == [""]
    assert [
        (event.event_id, event.actor_type, event.correlation_ids) for event in _decisions(audits)
    ] == [(f"exec-1:HUMAN_INTERVENTION_DECISION:{unkeyed_subject}", None, {})]
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_a_gate_update_resolves_only_the_park_the_run_waits_on():
    from temporalio.client import WorkflowUpdateFailedError

    alerts: list[AlertHumanInterventionRequest] = []
    audits: list[AuditEvent] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        alerts=alerts,
        audit_events=audits,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "apply_gate_decision",
                    args=["exec-1:review:security-review:1", True, "slack:E1/U1"],
                )
            park_key = await _await_park_key(handle, "review")
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "apply_gate_decision", args=[f"{park_key}-stale", True, "slack:E1/U1"]
                )
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "apply_repair_decision", args=[park_key, True, "slack:E1/U1"]
                )
            outcome = await handle.execute_update(
                "apply_gate_decision",
                args=[park_key, True, "slack:E1/U1", "slack", "group:S0123456789"],
                id=f"slack-decision:{park_key}",
                result_type=ParkDecisionOutcome,
            )
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert [alert.park_key for alert in alerts] == [park_key]
    assert outcome == ParkDecisionOutcome(
        decision="approved", actor="slack:E1/U1", actor_type="slack", applied=True
    )
    [decision] = _decisions(audits)
    assert (decision.decision, decision.actor_type, decision.actor_id) == (
        "approved",
        "slack",
        "slack:E1/U1",
    )
    assert decision.correlation_ids == {
        "park_key": park_key,
        "approval_basis": "group:S0123456789",
    }
    token = ActionToken(
        tenant="alpha",
        execution_id="exec-1",
        workflow_id="wf",
        park_key=park_key,
        kind="gate",
        actions=("approve",),
        expires_at=0,
    )
    assert token.decision_event_id == decision.event_id


async def test_review_and_policy_parks_on_the_same_gate_get_distinct_keys():
    from temporalio.client import WorkflowUpdateFailedError

    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        policy_allow=False,
        policy_human_review=["gate_failed:security-review"],
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_policy=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            review_key = await _await_park_key(handle, "review")
            await handle.execute_update(
                "apply_gate_decision", args=[review_key, True, "slack:E1/U1"]
            )
            policy_key = await _await_park_key(handle, "policy")
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "apply_gate_decision", args=[review_key, True, "slack:E1/U1"]
                )
            await handle.execute_update(
                "apply_gate_decision", args=[policy_key, True, "slack:E1/U1"]
            )
            result = await handle.result()

    assert review_key != policy_key
    assert result.status == PR_PROMOTED


async def test_a_repair_update_for_an_earlier_park_is_rejected_after_the_run_reparks():
    from temporalio.client import WorkflowUpdateFailedError

    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None, 1, [], [], [], ci_results=[FAILURE, FAILURE, FAILURE, FAILURE], alerts=alerts
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=1),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            first_key = await _await_park_key(handle, "repair")
            await handle.execute_update(
                "apply_repair_decision", args=[first_key, True, "slack:E1/U1"]
            )
            second_key = first_key
            for _ in range(100):
                second_key = await _await_park_key(handle, "repair")
                if second_key != first_key:
                    break
                await asyncio.sleep(0.05)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    "apply_repair_decision", args=[first_key, False, "slack:E1/U1"]
                )
            update_id = f"slack-decision:{second_key}"
            first, repeated = await asyncio.gather(
                handle.execute_update(
                    "apply_repair_decision",
                    args=[second_key, False, "slack:E1/U2"],
                    id=update_id,
                    result_type=ParkDecisionOutcome,
                ),
                handle.execute_update(
                    "apply_repair_decision",
                    args=[second_key, False, "slack:E1/U2"],
                    id=update_id,
                    result_type=ParkDecisionOutcome,
                ),
            )
            result = await handle.result()

    assert second_key != first_key
    assert [alert.park_key for alert in alerts] == [first_key, second_key, ""]
    assert first == repeated
    assert first.decision == "abandon"
    assert result.terminal_reason == "human_abandoned"


async def test_a_stray_wake_does_not_end_a_keyed_gate_park():
    audits: list[AuditEvent] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        security_verdict="block",
        review_on_block={"security-review": "pause_for_human"},
        audit_events=audits,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "review")
            await handle.signal("submit_pr_approval", args=[True, "someone"])
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            await asyncio.sleep(0.2)
            assert await _await_park_key(handle, "review") == park_key
            await handle.signal(
                "submit_gate_decision", args=["security-review", True, "security-lead"]
            )
            result = await handle.result()

    assert result.status == PR_PROMOTED
    [decision] = _decisions(audits)
    assert (decision.decision, decision.actor_type, decision.actor_id) == (
        "approved",
        "operator",
        "security-lead",
    )


@pytest.mark.parametrize(
    ("stage", "options", "workflow_input", "signal", "update", "earlier"),
    [
        (
            "review",
            {
                "ci_result": SUCCESS,
                "security_verdict": "block",
                "review_on_block": {"security-review": "pause_for_human"},
            },
            {"run_reviews": True},
            ("submit_gate_decision", ["security-review", True, "security-lead"]),
            "apply_gate_decision",
            ParkDecisionOutcome("approved", "security-lead", "operator", applied=False),
        ),
        (
            "repair",
            {"ci_results": [FAILURE, FAILURE, SUCCESS]},
            {"max_repair_attempts": 1},
            ("submit_repair_decision", [True, "release-manager"]),
            "apply_repair_decision",
            ParkDecisionOutcome("retry", "release-manager", "operator", applied=False),
        ),
    ],
)
async def test_an_update_that_meets_a_signalled_decision_returns_the_earlier_decision(
    stage, options, workflow_input, signal, update, earlier
):
    audits: list[AuditEvent] = []
    activities = _make_activities(None, 1, [], [], [], audit_events=audits, **options)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with _unsandboxed_worker(env, task_queue, activities, DraftPullRequestWorkflow):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(**workflow_input),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, stage)
        await handle.signal(signal[0], args=signal[1])
        pending = asyncio.create_task(
            handle.execute_update(
                update, args=[park_key, False, "slack:E1/U1"], result_type=ParkDecisionOutcome
            )
        )
        await asyncio.sleep(0.5)
        async with _unsandboxed_worker(env, task_queue, activities, DraftPullRequestWorkflow):
            outcome = await pending
            await handle.result()

    assert outcome == earlier
    [decision] = _decisions(audits)
    assert (decision.decision, decision.actor_id) == (earlier.decision, earlier.actor)


async def test_audit_event_ids_for_a_long_policy_gate_list_fit_the_audit_column():
    from dataclasses import replace

    from database.models.schema import audit_events

    gates = [
        "gate_failed:security-review-extended",
        "gate_failed:accessibility-conformance",
        "gate_failed:dependency-license-audit",
    ]
    audits: list[AuditEvent] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        policy_allow=False,
        policy_human_review=gates,
        audit_events=audits,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                replace(
                    _ci_input(run_reviews=True, run_policy=True), execution_id="intake-1a2b3c4d"
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "policy")
            await handle.execute_update("apply_gate_decision", args=[park_key, True, "slack:E1/U1"])
            await handle.result()

    longest = max(len(event.event_id) for event in audits)
    column_type = audit_events.c.event_id.type
    assert isinstance(column_type, String)
    assert longest > 64
    assert longest <= (column_type.length or 0)


def _failing_plan_model(
    activities: list[Callable[..., Any]], *, alert_raises: bool = False
) -> list[Callable[..., Any]]:
    from temporalio.exceptions import ApplicationError

    @activity.defn(name="invoke_model")
    async def failing_invoke_model(req: ModelRequest) -> ModelResponse:
        raise ApplicationError(
            "500 Internal Server Error", type="LiteLLMTransient", non_retryable=True
        )

    @activity.defn(name="alert_human_intervention")
    async def failing_alert(req: AlertHumanInterventionRequest) -> AlertHumanInterventionResult:
        raise ApplicationError("alert channels unreachable", non_retryable=True)

    replaced: dict[str, Callable[..., Any]] = {"fake_invoke_model": failing_invoke_model}
    if alert_raises:
        replaced["fake_alert"] = failing_alert
    return [replaced.get(fn.__name__, fn) for fn in activities]


def _root_cause(error: BaseException) -> BaseException:
    while error.__cause__ is not None:
        error = error.__cause__
    return error


async def test_a_run_whose_plan_review_fails_is_recorded_failed_and_alerted():
    from temporalio.client import WorkflowFailureError

    alerts: list[AlertHumanInterventionRequest] = []
    persisted: list[ExecutionState] = []
    audits: list[AuditEvent] = []
    finalizations: list[FinalizeClaimRequest] = []
    activities = _failing_plan_model(
        _make_activities(
            None,
            1,
            [],
            [],
            [],
            alerts=alerts,
            persisted_states=persisted,
            audit_events=audits,
            claim_finalizations=finalizations,
        )
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            with pytest.raises(WorkflowFailureError) as failed:
                await env.client.execute_workflow(
                    DraftPullRequestWorkflow.run,
                    _ci_input(run_planning=True),
                    id=workflow_id,
                    task_queue=task_queue,
                )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert str(_root_cause(failed.value)) == "LiteLLMTransient: 500 Internal Server Error"
    assert [(alert.reason, alert.stage) for alert in alerts] == [(RUN_FAILED, "plan")]
    assert alerts[0].detail == "LiteLLMTransient: 500 Internal Server Error"
    assert [(step.kind, step.issue_key) for step in alerts[0].remedy.actions] == [
        ("restart_run", "T-1")
    ]
    assert (persisted[-1].state, persisted[-1].workflow_id) == ("FAILED", workflow_id)
    failures = [event for event in audits if event.event_type == "RUN_FAILED"]
    assert [(event.stage, event.new_state, event.reason) for event in failures] == [
        ("plan", "FAILED", "LiteLLMTransient: 500 Internal Server Error")
    ]
    assert any("run-failed-alert" in marker for marker in _patch_markers(history))


async def test_a_failed_run_alert_never_replaces_the_run_failure():
    from temporalio.client import WorkflowFailureError

    persisted: list[ExecutionState] = []
    activities = _failing_plan_model(
        _make_activities(None, 1, [], [], [], persisted_states=persisted), alert_raises=True
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            with pytest.raises(WorkflowFailureError) as failed:
                await env.client.execute_workflow(
                    DraftPullRequestWorkflow.run,
                    _ci_input(run_planning=True),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    assert str(_root_cause(failed.value)) == "LiteLLMTransient: 500 Internal Server Error"
    assert persisted[-1].state == "FAILED"


@workflow.defn(name="DraftPullRequestWorkflow")
class _PreRunFailedAlertDraftPr(DraftPullRequestWorkflow):
    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        try:
            outcome = await self._orchestrate(params)
        except Exception:
            await self._finalize_claim(params)
            raise
        await self._finalize_claim(params)
        return outcome


async def test_a_run_failed_before_the_failure_alert_replays_without_it():
    from temporalio.client import WorkflowFailureError
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    alerts: list[AlertHumanInterventionRequest] = []
    activities = _failing_plan_model(_make_activities(None, 1, [], [], [], alerts=alerts))
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_PreRunFailedAlertDraftPr, *children],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
        ):
            with pytest.raises(WorkflowFailureError):
                await env.client.execute_workflow(
                    "DraftPullRequestWorkflow",
                    _ci_input(run_planning=True),
                    id=workflow_id,
                    task_queue=task_queue,
                    result_type=DraftPullRequestOutput,
                )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert alerts == []
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *children],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_child_workflows_record_the_run_workflow_on_the_execution():
    persisted: list[ExecutionState] = []
    activities = _make_activities(None, 1, [], [], [], persisted_states=persisted)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_planning=True),
                id=workflow_id,
                task_queue=task_queue,
            )

    assert {"PLANNING", "IMPLEMENTING", "PR_CREATED"} <= {state.state for state in persisted}
    assert {state.workflow_id for state in persisted} == {workflow_id}


def _model_path_outages(
    activities: list[Callable[..., Any]],
    hops: list[str],
    suspensions: list[RecordSuspensionRequest],
) -> list[Callable[..., Any]]:
    from services.orchestration.model_path import model_unavailable_error

    remaining = list(hops)

    @activity.defn(name="invoke_model")
    async def outage_invoke_model(req: ModelRequest) -> ModelResponse:
        if remaining:
            raise model_unavailable_error(remaining.pop(0), "litellm returned 500")
        return ModelResponse(
            logical_model=req.logical_model,
            content="Plan:\n- Edit feature.py to add the change",
            model_id="planning-model",
        )

    @activity.defn(name="record_suspension")
    async def recording_suspension(req: RecordSuspensionRequest) -> str:
        suspensions.append(req)
        return req.workflow_id

    replaced: dict[str, Callable[..., Any]] = {
        "fake_invoke_model": outage_invoke_model,
        "fake_record_suspension": recording_suspension,
    }
    return [replaced.get(fn.__name__, fn) for fn in activities]


async def _await_model_path_park(handle, previous_key: str = "") -> str:
    state: dict = {}
    for _ in range(200):
        state = await handle.query(DraftPullRequestWorkflow.suspension_state)
        key = state.get("park_key", "")
        if state.get("reason") == SuspendReason.MODEL_UNAVAILABLE.value and key not in (
            "",
            previous_key,
        ):
            return key
        await asyncio.sleep(0.05)
    raise AssertionError(f"no model path park: {state}")


def _plan_child_ids(history) -> list[str]:
    return [
        event.start_child_workflow_execution_initiated_event_attributes.workflow_id
        for event in history.events
        if event.HasField("start_child_workflow_execution_initiated_event_attributes")
        and "-plan-" in event.start_child_workflow_execution_initiated_event_attributes.workflow_id
    ]


class _OneRunParks:
    def __init__(self, client, workflow_id: str) -> None:
        from services.orchestration.model_path_parks import TemporalParkResumer

        self._resumer = TemporalParkResumer(client)
        self._workflow_id = workflow_id

    async def parked(self, tenant: str) -> list[str]:
        return [self._workflow_id]

    async def resume(self, workflow_id: str) -> bool:
        return await self._resumer.resume(
            workflow_id, actor="model_path_probe", actor_type="intake"
        )


async def test_a_plan_child_on_an_unavailable_model_path_parks_and_resumes_on_recovery():
    from services.coordination.idempotency import InMemorySideEffectLedger
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner
    from tests.support import ModelPathStub, model_path_gate

    alerts: list[AlertHumanInterventionRequest] = []
    persisted: list[ExecutionState] = []
    audits: list[AuditEvent] = []
    contexts: list[ContextRequest] = []
    suspensions: list[RecordSuspensionRequest] = []
    activities = _model_path_outages(
        _make_activities(
            None,
            1,
            [],
            [],
            [],
            alerts=alerts,
            persisted_states=persisted,
            audit_events=audits,
            context_requests=contexts,
        ),
        ["bedrock_path"],
        suspensions,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_planning=True),
                id=workflow_id,
                task_queue=task_queue,
            )
            park_key = await _await_model_path_park(handle)
            parked_state = persisted[-1].state
            gate = model_path_gate(
                ModelPathStub(""),
                ledger=InMemorySideEffectLedger(),
                parks=_OneRunParks(env.client, workflow_id),
            )
            await gate.sweep()
            result = await handle.result()
            history = await handle.fetch_history()

    assert result.status == PR_PROMOTED
    assert parked_state == "AWAITING_HUMAN"
    assert park_key == "exec-1:plan:model_unavailable:1"
    assert [(alert.reason, alert.stage, alert.park_key) for alert in alerts] == [
        ("model_unavailable", "plan", "")
    ]
    assert [(step.kind, step.key, step.issue_key) for step in alerts[0].remedy.actions] == [
        ("restore_model_path", "bedrock_path", ""),
        ("resume_parked", "", "T-1"),
    ]
    assert [(req.reason, req.stage, req.detail) for req in suspensions] == [
        ("model_unavailable", "plan", "bedrock_path")
    ]
    assert len(contexts) == 1
    assert _plan_child_ids(history) == [f"{workflow_id}-plan-0", f"{workflow_id}-plan-1"]
    resumed = [event for event in audits if event.event_type == "MODEL_PATH_RESUMED"]
    assert [(event.actor_type, event.correlation_ids) for event in resumed] == [
        ("intake", {"park_key": park_key})
    ]
    assert not [event for event in audits if event.event_type == "RUN_FAILED"]
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_a_second_outage_parks_again_under_a_new_key_that_an_earlier_resume_cannot_end():
    from services.orchestration.model_path_parks import TemporalParkResumer
    from services.orchestration.suspension import RESUME_MODEL_PATH_UPDATE
    from temporalio.client import WorkflowUpdateFailedError

    alerts: list[AlertHumanInterventionRequest] = []
    suspensions: list[RecordSuspensionRequest] = []
    activities = _model_path_outages(
        _make_activities(None, 1, [], [], [], alerts=alerts),
        ["bedrock_path", "credentials"],
        suspensions,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        resumer = TemporalParkResumer(env.client)
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_planning=True),
                id=workflow_id,
                task_queue=task_queue,
            )
            first = await _await_model_path_park(handle)
            assert await resumer.resume(workflow_id, actor="op", actor_type="operator")
            second = await _await_model_path_park(handle, previous_key=first)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    RESUME_MODEL_PATH_UPDATE, args=[first, "op", "operator"]
                )
            assert await resumer.resume(workflow_id, actor="op", actor_type="operator")
            result = await handle.result()
            history = await handle.fetch_history()

    assert result.status == PR_PROMOTED
    assert (first, second) == (
        "exec-1:plan:model_unavailable:1",
        "exec-1:plan:model_unavailable:2",
    )
    assert [alert.remedy.actions[0].key for alert in alerts] == ["bedrock_path", "credentials"]
    assert [req.detail for req in suspensions] == ["bedrock_path", "credentials"]
    assert _plan_child_ids(history) == [f"{workflow_id}-plan-{n}" for n in range(3)]


async def test_a_model_path_park_outlives_a_worker_restart_and_resumes_once():
    from services.orchestration.model_path_parks import TemporalParkResumer

    audits: list[AuditEvent] = []
    activities = _model_path_outages(
        _make_activities(None, 1, [], [], [], audit_events=audits), ["litellm"], []
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with _unsandboxed_worker(env, task_queue, activities, DraftPullRequestWorkflow):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_planning=True),
                id=workflow_id,
                task_queue=task_queue,
            )
            await _await_model_path_park(handle)
        resumer = TemporalParkResumer(env.client)
        async with _unsandboxed_worker(env, task_queue, activities, DraftPullRequestWorkflow):
            outcomes = await asyncio.gather(
                resumer.resume(workflow_id, actor="op", actor_type="operator"),
                resumer.resume(workflow_id, actor="intake", actor_type="intake"),
            )
            late = await resumer.resume(workflow_id, actor="op", actor_type="operator")
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert sorted(outcomes) == [False, True]
    assert late is False
    assert len([event for event in audits if event.event_type == "MODEL_PATH_RESUMED"]) == 1


async def test_a_resume_while_the_suspension_is_still_clearing_is_refused():
    from services.orchestration.model_path_parks import TemporalParkResumer

    clearing, release = asyncio.Event(), asyncio.Event()

    @activity.defn(name="clear_suspension")
    async def slow_clear_suspension(req: ClearSuspensionRequest) -> str:
        clearing.set()
        await release.wait()
        return req.workflow_id

    activities = [
        slow_clear_suspension if getattr(fn, "__name__", "") == "fake_clear_suspension" else fn
        for fn in _model_path_outages(_make_activities(None, 1, [], [], []), ["litellm"], [])
    ]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with _unsandboxed_worker(env, task_queue, activities, DraftPullRequestWorkflow):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_planning=True),
                id=workflow_id,
                task_queue=task_queue,
            )
            await _await_model_path_park(handle)
            resumer = TemporalParkResumer(env.client)
            first = await resumer.resume(workflow_id, actor="intake", actor_type="intake")
            await clearing.wait()
            second = await resumer.resume(workflow_id, actor="op", actor_type="operator")
            release.set()
            result = await handle.result()

    assert (first, second) == (True, False)
    assert result.status == PR_PROMOTED


@workflow.defn(name="DraftPullRequestWorkflow")
class _PreModelPathParkDraftPr(DraftPullRequestWorkflow):
    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        return await super().run(params)

    def _model_path_parks_enabled(self) -> bool:
        return False


async def test_a_run_without_the_model_path_park_patch_fails_and_replays_as_before():
    from temporalio.client import WorkflowFailureError
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    alerts: list[AlertHumanInterventionRequest] = []
    persisted: list[ExecutionState] = []
    activities = _model_path_outages(
        _make_activities(None, 1, [], [], [], alerts=alerts, persisted_states=persisted),
        ["bedrock_path"],
        [],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        async with _unsandboxed_worker(env, task_queue, activities, _PreModelPathParkDraftPr):
            with pytest.raises(WorkflowFailureError) as failed:
                await env.client.execute_workflow(
                    "DraftPullRequestWorkflow",
                    _ci_input(run_planning=True),
                    id=workflow_id,
                    task_queue=task_queue,
                    result_type=DraftPullRequestOutput,
                )
            history = await env.client.get_workflow_handle(workflow_id).fetch_history()

    assert str(_root_cause(failed.value)).startswith("ModelUnavailable: ")
    assert [(alert.reason, alert.stage) for alert in alerts] == [(RUN_FAILED, "plan")]
    assert persisted[-1].state == "FAILED"
    assert not any("model-path-park" in marker for marker in _patch_markers(history))
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


_ORIGINAL_TS = "1700000000.000100"


def _park_post_activities(refreshes: list, settles: list, alert_times: list) -> list:
    buttons = json.dumps([{"type": "section"}, {"type": "actions", "elements": []}])

    @activity.defn(name="alert_human_intervention")
    async def fake_alert(req: AlertHumanInterventionRequest) -> AlertHumanInterventionResult:
        scheduled = activity.info().scheduled_time
        alert_times.append(scheduled)
        return AlertHumanInterventionResult(
            channels=["slack"],
            park_post=ParkPost("C1", _ORIGINAL_TS, buttons) if req.park_key else None,
            issued_at=int(scheduled.timestamp()),
            ttl_hours=72,
        )

    @activity.defn(name="refresh_park_buttons")
    async def fake_refresh(req: RefreshParkButtonsRequest) -> RefreshParkButtonsResult:
        scheduled = activity.info().scheduled_time
        refreshes.append((scheduled, req))
        fresh = ParkPost("C1", f"thread.{len(refreshes)}", buttons)
        return RefreshParkButtonsResult(
            posts=[*(replace(post, blocks="[]") for post in req.posts), fresh],
            issued_at=int(scheduled.timestamp()),
            ttl_hours=72,
        )

    @activity.defn(name="settle_park_posts")
    async def fake_settle(req: SettleParkPostsRequest) -> None:
        settles.append(req)

    activities = [
        activity_fn
        for activity_fn in _make_activities(None, 1, [], [], [], ci_result=SUCCESS, plan_pause=True)
        if activity_fn.__temporal_activity_definition.name != "alert_human_intervention"
    ]
    return [*activities, fake_alert, fake_refresh, fake_settle]


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the condition did not hold")


async def test_a_park_posts_fresh_buttons_in_its_thread_before_each_expiry_until_decided():
    refreshes: list = []
    settles: list = []
    alert_times: list = []
    activities = _park_post_activities(refreshes, settles, alert_times)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "plan")
            await env.sleep(timedelta(hours=67))
            assert refreshes == []
            await env.sleep(timedelta(hours=2))
            await _until(lambda: len(refreshes) == 1)
            await env.sleep(timedelta(hours=68))
            await _until(lambda: len(refreshes) == 2)
            await handle.execute_update(
                "apply_gate_decision", args=[park_key, True, "slack:E1/U-LEAD"]
            )
            result = await handle.result()

    assert result.status == PR_PROMOTED
    [alerted] = alert_times
    (first_at, first), (second_at, second) = refreshes
    lead_before_expiry = 68 * 3600
    assert 0 <= first_at.timestamp() - int(alerted.timestamp()) - lead_before_expiry < 60
    assert 0 <= second_at.timestamp() - int(first_at.timestamp()) - lead_before_expiry < 60
    assert first.reminder and second.reminder
    assert first.alert.park_key == park_key
    assert [post.ts for post in first.posts] == [_ORIGINAL_TS]
    assert [post.ts for post in second.posts] == [_ORIGINAL_TS, "thread.1"]
    [settled] = settles
    assert (settled.decision, settled.actor) == ("approved", "slack:E1/U-LEAD")
    assert [post.ts for post in settled.posts] == [_ORIGINAL_TS, "thread.1", "thread.2"]


async def test_a_park_decided_before_its_reminder_posts_no_reminder():
    refreshes: list = []
    settles: list = []
    activities = _park_post_activities(refreshes, settles, [])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "plan")
            await handle.execute_update("apply_gate_decision", args=[park_key, False, "slack:E1/U"])
            await handle.result()

    assert refreshes == []
    assert [(settle.decision, [post.ts for post in settle.posts]) for settle in settles] == [
        ("rejected", [_ORIGINAL_TS])
    ]


async def test_repeated_refresh_requests_post_one_set_of_fresh_buttons_a_minute():
    refreshes: list = []
    settles: list = []
    activities = _park_post_activities(refreshes, settles, [])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(run_reviews=True, run_planning=True),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            park_key = await _await_park_key(handle, "plan")
            asked = [
                await handle.execute_update("refresh_park_buttons", args=[park_key])
                for _ in range(3)
            ]
            await _until(lambda: len(refreshes) == 1)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("refresh_park_buttons", args=[f"{park_key}0"])
            await env.sleep(timedelta(minutes=2))
            asked.append(await handle.execute_update("refresh_park_buttons", args=[park_key]))
            await _until(lambda: len(refreshes) == 2)
            await handle.execute_update("apply_gate_decision", args=[park_key, True, "slack:E1/U"])
            await handle.result()

    assert asked == [True, False, False, True]
    assert [request.reminder for _, request in refreshes] == [False, False]
    [settled] = settles
    assert [post.ts for post in settled.posts] == [_ORIGINAL_TS, "thread.1", "thread.2"]


async def test_a_park_history_without_the_button_refresh_patch_replays(monkeypatch):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    patch_id = "park-button-refresh"
    refreshes: list = []
    settles: list = []
    activities = _park_post_activities(refreshes, settles, [])
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]
    patched = workflow.patched

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as pre_patch:
            pre_patch.setattr(workflow, "patched", lambda name: name != patch_id and patched(name))
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[DraftPullRequestWorkflow, *children],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=activities,
            ):
                handle = await env.client.start_workflow(
                    DraftPullRequestWorkflow.run,
                    _ci_input(run_reviews=True, run_planning=True),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                park_key = await _await_park_key(handle, "plan")
                with pytest.raises(WorkflowUpdateFailedError):
                    await handle.execute_update("refresh_park_buttons", args=[park_key])
                await handle.execute_update(
                    "apply_gate_decision", args=[park_key, True, "slack:E1/U"]
                )
                await handle.result()
                history = await handle.fetch_history()

    assert not any(patch_id in marker for marker in _patch_markers(history))
    assert (refreshes, settles) == ([], [])
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *children],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def _run_capturing_limits(monkeypatch, *, patched_limits: bool):
    from temporalio.worker import UnsandboxedWorkflowRunner

    reads: list[RunLimitsRequest] = []
    granted = RunLimits(max_cost_usd=7.5, max_llm_calls=250, max_tokens=2_500_000)
    activities = _make_activities(None, 1, [], [], [], run_limits=granted, limit_reads=reads)
    children = [ImplementationWorkflow, BranchSyncWorkflow, PullRequestWorkflow, PlanReviewWorkflow]
    params = _ci_input()
    child_budgets: list[tuple[float, int, int]] = []
    execute_child_workflow = workflow.execute_child_workflow
    patched = workflow.patched

    async def capture_budget(child, arg, **kwargs):
        if child == ImplementationWorkflow.run:
            child_budgets.append((arg.max_cost_usd, arg.max_llm_calls, arg.max_tokens))
        return await execute_child_workflow(child, arg, **kwargs)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as run:
            run.setattr(workflow, "execute_child_workflow", capture_budget)
            if not patched_limits:
                run.setattr(
                    workflow, "patched", lambda name: name != "limit-grants" and patched(name)
                )
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[DraftPullRequestWorkflow, *children],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=activities,
            ):
                handle = await env.client.start_workflow(
                    DraftPullRequestWorkflow.run,
                    params,
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                await handle.result()
                history = await handle.fetch_history()
    return params, reads, child_budgets, history, children


async def test_the_implementation_runs_under_the_limits_the_grant_activity_returns(monkeypatch):
    params, reads, child_budgets, history, _ = await _run_capturing_limits(
        monkeypatch, patched_limits=True
    )

    assert reads[0].execution_id == params.execution_id
    assert reads[0].configured == RunLimits(
        max_cost_usd=params.max_cost_usd,
        max_llm_calls=params.max_llm_calls,
        max_tokens=params.max_tokens,
    )
    assert child_budgets == [(7.5, 250, 2_500_000)]
    assert "limit-grants" in " ".join(_patch_markers(history))


async def test_a_history_without_the_limit_grants_patch_replays_under_the_configured_limits(
    monkeypatch,
):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    params, reads, child_budgets, history, children = await _run_capturing_limits(
        monkeypatch, patched_limits=False
    )

    assert reads == []
    assert child_budgets == [(params.max_cost_usd, params.max_llm_calls, params.max_tokens)]
    assert not any("limit-grants" in marker for marker in _patch_markers(history))
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *children],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


def _granted_cost(activities, granted: dict[str, float], reads: list[RunLimitsRequest]):
    @activity.defn(name="read_run_limits")
    async def fake_read_run_limits(req: RunLimitsRequest) -> RunLimits:
        reads.append(req)
        if "cost" in granted:
            return replace(req.configured, max_cost_usd=granted["cost"])
        return req.configured

    return [
        fake_read_run_limits if fn.__name__ == "fake_read_run_limits" else fn for fn in activities
    ]


async def test_a_run_at_its_cost_limit_parks_and_a_grant_reruns_the_repair_under_the_new_limit():
    from services.orchestration.suspension import RESUME_LIMIT_PARK_UPDATE
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    repairs: list[ApplyCIRepairRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    audits: list[AuditEvent] = []
    persisted: list[ExecutionState] = []
    reads: list[RunLimitsRequest] = []
    granted: dict[str, float] = {}
    activities = _granted_cost(
        _make_activities(
            None,
            1,
            [],
            [],
            [],
            ci_result=FAILURE,
            repairs=repairs,
            alerts=alerts,
            audit_events=audits,
            persisted_states=persisted,
        ),
        granted,
        reads,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=20, max_cost_usd=0.35),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            first = await _await_limit_park(handle)
            repairs_at_first_park = len(repairs)
            parked_state = persisted[-1].state
            granted["cost"] = 0.65
            resumed = await handle.execute_update(
                RESUME_LIMIT_PARK_UPDATE, args=[first["park_key"], "slack:E1/UADMIN", "limit_grant"]
            )
            second = await _await_limit_park(handle, previous_key=first["park_key"])
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    RESUME_LIMIT_PARK_UPDATE, args=[first["park_key"], "slack:E1/UADMIN"]
                )
            await handle.execute_update(
                "apply_repair_decision", args=[second["park_key"], False, "slack:E1/UTENANT"]
            )
            result = await handle.result()
            history = await handle.fetch_history()

    assert parked_state == "AWAITING_HUMAN"
    assert first["detail"] == "IMPLEMENTATION_MAX_COST_USD"
    assert resumed["applied"] is True
    assert repairs_at_first_park == 3
    assert len(repairs) == 6
    assert result.terminal_reason == "human_abandoned"
    assert persisted[-1].state == "CANCELLED"
    assert [alert.park_key for alert in alerts] == [first["park_key"], second["park_key"], ""]
    assert "Usage: 0.4 of 0.35 USD" in alerts[0].detail
    decisions = [event for event in audits if event.event_type == "LIMIT_PARK_DECISION"]
    assert [(event.decision, event.actor_id) for event in decisions] == [
        ("resume", "slack:E1/UADMIN"),
        ("human_abandoned", "slack:E1/UTENANT"),
    ]
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_a_limit_park_posts_fresh_buttons_before_expiry_and_settles_its_posts_on_a_grant():
    from services.orchestration.suspension import RESUME_LIMIT_PARK_UPDATE

    refreshes: list = []
    settles: list = []
    alerts: list = []
    reads: list[RunLimitsRequest] = []
    granted: dict[str, float] = {}
    park_fakes = _park_post_activities(refreshes, settles, alerts)[-3:]
    faked = {fn.__temporal_activity_definition.name for fn in park_fakes}
    activities = [
        fn
        for fn in _granted_cost(
            _make_activities(None, 1, [], [], [], ci_result=FAILURE), granted, reads
        )
        if fn.__temporal_activity_definition.name not in faked
    ]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, [*activities, *park_fakes]):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(max_repair_attempts=20, max_cost_usd=0.35),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            first = await _await_limit_park(handle)
            await env.sleep(timedelta(hours=67))
            assert refreshes == []
            await env.sleep(timedelta(hours=2))
            await _until(lambda: len(refreshes) == 1)
            granted["cost"] = 0.65
            await handle.execute_update(
                RESUME_LIMIT_PARK_UPDATE, args=[first["park_key"], "slack:E1/UADMIN", "limit_grant"]
            )
            second = await _await_limit_park(handle, previous_key=first["park_key"])
            await handle.execute_update(
                "apply_repair_decision", args=[second["park_key"], False, "slack:E1/UTENANT"]
            )
            await handle.result()

    [(_, refresh)] = refreshes
    assert refresh.reminder
    assert (refresh.alert.park_key, refresh.alert.limit_usage) == (first["park_key"], 0.4)
    granted_settle, abandoned_settle = settles
    assert (granted_settle.decision, granted_settle.actor) == ("granted", "slack:E1/UADMIN")
    assert [post.ts for post in granted_settle.posts] == [_ORIGINAL_TS, "thread.1"]
    assert (abandoned_settle.decision, abandoned_settle.actor) == ("abandon", "slack:E1/UTENANT")


async def test_a_run_without_the_limit_park_patch_stops_at_its_limit_and_replays(monkeypatch):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(None, 1, [], [], [], ci_result=FAILURE, alerts=alerts)
    patched = workflow.patched

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as run:
            run.setattr(workflow, "patched", lambda name: name != "limit-park" and patched(name))
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=activities,
            ):
                handle = await env.client.start_workflow(
                    DraftPullRequestWorkflow.run,
                    _ci_input(max_repair_attempts=10, max_cost_usd=0.35),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                state = await _await_human_park(handle)
                await handle.signal("submit_repair_decision", args=[False, "release-manager"])
                result = await handle.result()
                history = await handle.fetch_history()

    assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
    assert result.terminal_reason == "human_abandoned"
    assert [alert.reason for alert in alerts] == ["cost_budget_exceeded", "human_abandoned"]
    assert not any("limit-park" in marker for marker in _patch_markers(history))
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


@pytest.mark.parametrize(
    ("actor", "actor_type"),
    [("slack:E1/UADMIN", "limit_grant"), ("period_reset", "period_reset")],
    ids=["period grant", "period reset"],
)
async def test_a_spent_period_budget_parks_the_run_at_its_next_stage_until_a_resume(
    actor, actor_type
):
    from services.orchestration.suspension import RESUME_LIMIT_PARK_UPDATE
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    commits: list[CommitPushRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    settles: list[SettleParkPostsRequest] = []
    audits: list[AuditEvent] = []
    reads: list[TenantBudgetRequest] = []
    budget = {"spent": True}
    buttons = json.dumps([{"type": "section"}, {"type": "actions", "elements": []}])

    @activity.defn(name="alert_human_intervention")
    async def fake_alert(req: AlertHumanInterventionRequest) -> AlertHumanInterventionResult:
        alerts.append(req)
        return AlertHumanInterventionResult(
            channels=["slack"],
            park_post=ParkPost("CT", "reply.1", buttons, thread_ts=req.thread_ts)
            if req.park_key
            else None,
            issued_at=int(activity.info().scheduled_time.timestamp()),
            ttl_hours=72,
        )

    @activity.defn(name="settle_park_posts")
    async def fake_settle(req: SettleParkPostsRequest) -> None:
        settles.append(req)

    faked = {"alert_human_intervention", "settle_park_posts"}
    activities = [
        fn
        for fn in _make_activities(
            None,
            1,
            commits,
            [],
            [],
            audit_events=audits,
            tenant_budget=budget,
            budget_reads=reads,
            period_thread=PeriodAlertThread(channel="CT", ts="100.1"),
        )
        if fn.__temporal_activity_definition.name not in faked
    ] + [fake_alert, fake_settle]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            parked = await _await_limit_park(handle)
            commits_while_parked = len(commits)
            budget["spent"] = False
            resumed = await handle.execute_update(
                RESUME_LIMIT_PARK_UPDATE, args=[parked["park_key"], actor, actor_type]
            )
            result = await handle.result()
            history = await handle.fetch_history()

    assert (parked["stage"], parked["detail"]) == ("implementation", "TENANT_MODEL_BUDGET_USD")
    assert commits_while_parked == 0
    assert resumed["applied"] is True
    assert commits
    assert result.terminal_reason != "human_abandoned"
    [park_alert] = [alert for alert in alerts if alert.park_key]
    assert (park_alert.stage, park_alert.thread_ts) == ("implementation", "100.1")
    assert "Usage: 100 of 100 USD this period" in park_alert.detail
    [parked_event] = [event for event in audits if event.event_type == "LIMIT_PARKED"]
    assert parked_event.correlation_ids["period_alert_ts"] == "100.1"
    [settle] = settles
    assert settle.decision == ("period_reset" if actor_type == "period_reset" else "granted")
    assert [(post.ts, post.thread_ts) for post in settle.posts] == [("reply.1", "100.1")]
    [decision] = [event for event in audits if event.event_type == "LIMIT_PARK_DECISION"]
    assert (decision.decision, decision.actor_id, decision.actor_type) == (
        "resume",
        actor,
        actor_type,
    )
    assert len(reads) >= 3
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


async def test_a_run_without_the_period_budget_patch_reads_no_budget_and_replays(monkeypatch):
    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

    commits: list[CommitPushRequest] = []
    reads: list[TenantBudgetRequest] = []
    activities = _make_activities(
        None, 1, commits, [], [], tenant_budget={"spent": True}, budget_reads=reads
    )
    patched = workflow.patched

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        with monkeypatch.context() as run:
            run.setattr(
                workflow, "patched", lambda name: name != "period-budget-park" and patched(name)
            )
            async with await _start_ci_worker(env, task_queue, activities):
                handle = await env.client.start_workflow(
                    DraftPullRequestWorkflow.run,
                    _ci_input(),
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                result = await handle.result()
                history = await handle.fetch_history()

    assert reads == []
    assert commits
    assert result.terminal_reason != "human_abandoned"
    replayed = await Replayer(
        workflows=[DraftPullRequestWorkflow, *_CHILD_WORKFLOWS],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert replayed.replay_failure is None


def _pushed(sha: str, *outside: str) -> CommitPushResult:
    return CommitPushResult(
        branch="agent/T-1",
        commit_sha=sha,
        pushed=True,
        replayed=False,
        excluded_paths=list(outside),
    )


_HELD = CommitPushResult(
    branch="agent/T-1",
    commit_sha="a" * 40,
    pushed=False,
    replayed=False,
    excluded_paths=["tsconfig.build.json"],
    changed=False,
    held_path="tsconfig.build.json",
    held_by_task_id="T-2",
    held_by_work_unit="unit-1",
)


def _remedy_text(alert: AlertHumanInterventionRequest) -> str:
    return "".join(step.text for step in alert.remedy.steps)


async def test_a_file_another_unit_holds_parks_the_commit_and_approve_commits_again():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    audits: list[AuditEvent] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        [],
        ci_result=SUCCESS,
        alerts=alerts,
        audit_events=audits,
        commit_results=[_HELD, _pushed("c" * 40, "tsconfig.build.json")],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park_stage(handle, "commit")
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            assert len(commits) == 1
            await handle.signal("submit_gate_decision", args=["file_claim", True, "lead"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert [(c.check_claims, c.work_unit) for c in commits] == [(True, "unit-1")] * 2
    parked = [alert for alert in alerts if alert.stage == "commit"]
    assert len(parked) == 1
    assert "tsconfig.build.json" in _remedy_text(parked[0])
    assert "unit-1 of T-2" in _remedy_text(parked[0])
    assert {step.kind for step in parked[0].remedy.actions} == {
        StepKind.APPROVE_GATE,
        StepKind.REJECT_GATE,
    }
    outside = [event for event in audits if event.event_type == "FILES_OUTSIDE_FILE_IMPACT"]
    assert [(event.event_id, event.artifact_hashes) for event in outside] == [
        ("exec-1:FILES_OUTSIDE_FILE_IMPACT:102", {"files": "tsconfig.build.json"})
    ]
    assert "- `tsconfig.build.json`" in pr_opens[0].body


_GUARDED = CommitPushResult(
    branch="agent/T-1",
    commit_sha="a" * 40,
    pushed=False,
    replayed=False,
    changed=False,
    guarded_paths=[".github/workflows/ci.yml"],
    guarded_diff="+++ b/.github/workflows/ci.yml\n+on: push\n",
)


async def test_a_workflow_file_parks_the_commit_and_approve_pushes_it():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        [],
        ci_result=SUCCESS,
        alerts=alerts,
        commit_results=[_GUARDED, _pushed("c" * 40)],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "commit")
            assert len(commits) == 1
            await handle.signal("submit_gate_decision", args=["workflow_files", True, "lead"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert [(c.guard_workflow_files, c.workflow_files_approved) for c in commits] == [
        (True, False),
        (True, True),
    ]
    parked = [alert for alert in alerts if alert.stage == "commit"]
    assert len(parked) == 1
    assert ".github/workflows/ci.yml" in _remedy_text(parked[0])
    assert "+on: push" in parked[0].detail
    assert {step.kind for step in parked[0].remedy.actions} == {
        StepKind.APPROVE_GATE,
        StepKind.REJECT_GATE,
    }


async def test_rejecting_a_workflow_file_park_abandons_the_run_without_a_push():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    activities = _make_activities(None, 1, commits, pr_opens, [], commit_results=[_GUARDED])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "commit")
            await handle.signal("submit_gate_decision", args=["workflow_files", False, "lead"])
            result = await handle.result()

    assert result.terminal_reason == HUMAN_ABANDONED
    assert len(commits) == 1
    assert pr_opens == []


_BLOCKED = DependencyStepResult(
    dependency_files=["package-lock.json", "package.json"],
    lockfiles=["package-lock.json"],
    blocking_count=1,
    threshold="high",
    blocking=["MAL-2026-14698 critical package-lock.json:3layerdipstackbndk47@0.1.4"],
)


async def test_a_blocking_dependency_finding_parks_before_the_commit_and_approve_commits():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    requests: list[DependencyStepRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        [],
        ci_result=SUCCESS,
        alerts=alerts,
        dependency_requests=requests,
        dependency_results=[_BLOCKED],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "commit")
            assert commits == []
            await handle.signal("submit_gate_decision", args=["dependency_scan", True, "lead"])
            result = await handle.result()

    assert result.status == PR_PROMOTED
    assert len(commits) == 1
    assert [(r.commit, r.base_ref) for r in requests] == [(commits[0].commit, _ci_input().base_sha)]
    parked = [alert for alert in alerts if alert.stage == "commit"]
    assert "MAL-2026-14698" in parked[0].detail


async def test_a_failed_lock_command_parks_and_reject_abandons_the_run_without_a_commit():
    commits: list[CommitPushRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
    failed = DependencyStepResult(
        dependency_files=["package.json"],
        lock_failed=True,
        lock_detail="`npm install --package-lock-only` for package.json exited 1.\nnpm error 404",
    )
    activities = _make_activities(
        None, 1, commits, [], [], alerts=alerts, dependency_results=[failed]
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "commit")
            await handle.signal("submit_gate_decision", args=["lockfile", False, "lead"])
            result = await handle.result()

    assert result.terminal_reason == HUMAN_ABANDONED
    assert commits == []
    parked = [alert for alert in alerts if alert.stage == "commit"]
    assert "npm error 404" in parked[0].detail
    assert "package.json" in _remedy_text(parked[0])


async def test_an_approved_lockfile_park_still_parks_on_the_dependency_scan():
    commits: list[CommitPushRequest] = []
    failed_and_blocked = replace(
        _BLOCKED, lockfiles=[], lock_failed=True, lock_detail="npm error 404"
    )
    activities = _make_activities(None, 1, commits, [], [], dependency_results=[failed_and_blocked])

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "commit")
            await handle.signal("submit_gate_decision", args=["lockfile", True, "lead"])
            for _ in range(50):
                state = await handle.query(DraftPullRequestWorkflow.suspension_state)
                if ":dependency_scan:" in state.get("park_key", ""):
                    break
                await asyncio.sleep(0.05)
            assert ":dependency_scan:" in state.get("park_key", "")
            assert commits == []
            await handle.signal("submit_gate_decision", args=["dependency_scan", False, "lead"])
            result = await handle.result()

    assert result.terminal_reason == HUMAN_ABANDONED
    assert commits == []


async def test_a_repair_commit_runs_the_dependency_step_again():
    commits: list[CommitPushRequest] = []
    requests: list[DependencyStepRequest] = []
    activities = _make_activities(
        None,
        1,
        commits,
        [],
        [],
        ci_results=[FAILURE, SUCCESS],
        repairs=[],
        dependency_requests=requests,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert [r.commit for r in requests] == [c.commit for c in commits]
    assert len(requests) == 2


async def test_rejecting_a_held_file_park_abandons_the_run_without_a_commit():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    ci_triggers: list = []
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        [],
        ci_triggers=ci_triggers,
        commit_results=[_HELD],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            await _await_human_park_stage(handle, "commit")
            await handle.signal("submit_gate_decision", args=["file_claim", False, "lead"])
            result = await handle.result()

    assert result.terminal_reason == HUMAN_ABANDONED
    assert len(commits) == 1
    assert ci_triggers == []
    assert pr_opens == []


async def test_a_repair_commit_on_a_held_file_parks_and_reject_abandons_the_run():
    commits: list[CommitPushRequest] = []
    ci_triggers: list = []
    activities = _make_activities(
        None,
        1,
        commits,
        [],
        [],
        ci_triggers=ci_triggers,
        ci_results=[FAILURE, SUCCESS],
        commit_results=[_pushed("c" * 40), _HELD],
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_human_park_stage(handle, "commit")
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value, state
            await handle.signal("submit_gate_decision", args=["file_claim", False, "lead"])
            result = await handle.result()

    assert result.terminal_reason == HUMAN_ABANDONED
    assert len(commits) == 2
    assert len(ci_triggers) == 1


async def test_a_commit_inside_the_file_impact_records_no_outside_files_event():
    audits: list[AuditEvent] = []
    pr_opens: list[OpenPrRequest] = []
    activities = _make_activities(None, 1, [], pr_opens, [], ci_result=SUCCESS, audit_events=audits)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == PR_PROMOTED
    assert not any(event.event_type == "FILES_OUTSIDE_FILE_IMPACT" for event in audits)
    assert "file impact" not in pr_opens[0].body
