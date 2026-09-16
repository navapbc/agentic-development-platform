"""Composition test: the draft-PR orchestrator chains implementation, sync, commit, and PR."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from openhands.runtime.caps import COMPLETED
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
    ImplementationRequest,
    ImplementationResult,
    ResetCoordinationRequest,
    ResetCoordinationResult,
)
from services.orchestration.temporal_activities.manifest import RecordManifestRequest
from services.orchestration.temporal_activities.notify import (
    AlertHumanInterventionRequest,
    AlertHumanInterventionResult,
)
from services.orchestration.temporal_activities.persistence import (
    AuditEvent,
    ExecutionState,
    UsageMetrics,
)
from services.orchestration.temporal_activities.planning import ContextRequest, ContextResult
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
    PLAN_REJECTED,
    POLICY_DENIED,
    PR_PROMOTED,
    PROMOTION_CANCELLED,
    REVIEW_BLOCKED,
    DraftPullRequestInput,
    DraftPullRequestWorkflow,
)
from services.orchestration.temporal_workflows.implementation import ImplementationWorkflow
from services.orchestration.temporal_workflows.pull_request import PullRequestWorkflow
from services.orchestration.temporal_workflows.review import PlanReviewWorkflow
from temporalio import activity
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
    alerts: list[AlertHumanInterventionRequest] | None = None,
    usage: list[UsageMetrics] | None = None,
    review_requests: list[GateContext] | None = None,
    code_verdict: str = "pass",
    security_verdict: str = "pass",
    acceptance_verdict: str = "pass",
    plan_verdict: str = "pass",
    impl_requests: list[ImplementationRequest] | None = None,
    context_requests: list[ContextRequest] | None = None,
    scan_requests: list[ScanChangeRequest] | None = None,
    scan_blocking_findings: int = 0,
    scan_result_findings: list[ScanFindingRecord] | None = None,
    scan_result_severity_counts: dict[str, int] | None = None,
    scan_change_raises: bool = False,
    policy_requests: list[EvaluatePromotionRequest] | None = None,
    policy_allow: bool = True,
    policy_reasons: list[str] | None = None,
    approvals: list[RecordApprovalRequest] | None = None,
    promotions: list[PromotePrRequest] | None = None,
    recheck_requires_pause: bool = False,
    recheck_category: str = "",
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
):
    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
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

    @activity.defn(name="commit_and_push")
    async def fake_commit(req: CommitPushRequest) -> CommitPushResult:
        commits.append(req)
        sha = "c" * 40
        if commit_shas:
            sha = commit_shas[min(len(commits) - 1, len(commit_shas) - 1)]
        return CommitPushResult(branch="agent/T-1", commit_sha=sha, pushed=True, replayed=False)

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
        outcome = (
            ci_results[min(idx, len(ci_results) - 1)] if ci_results is not None else ci_result
        )
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
            applied=True, changed_files=["feature.py"], cost=0.1, tokens=5, llm_calls=1
        )

    @activity.defn(name="alert_human_intervention")
    async def fake_alert(req: AlertHumanInterventionRequest) -> AlertHumanInterventionResult:
        if alerts is not None:
            alerts.append(req)
        return AlertHumanInterventionResult(channels=["jira"])

    @activity.defn(name="reset_task_coordination")
    async def fake_reset(req: ResetCoordinationRequest) -> ResetCoordinationResult:
        return ResetCoordinationResult(released=True, sequence_reset=True)

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
    async def fake_load_gate_plan() -> GatePlan:
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
        fake_claim,
        fake_find_open_pr,
        fake_stage,
        fake_sync,
        fake_resolve,
        fake_recheck,
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
    assert pr_opens[0].sequence == 103
    assert len(linkbacks) == 1


async def test_reclaimed_execution_uses_higher_gate_sequences():
    result, commits, pr_opens, _ = await _run(claim_version=2)

    assert result.status == PR_PROMOTED
    assert commits[0].sequence == 202
    assert pr_opens[0].sequence == 203


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
    assert len(alerts) == 1


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
    assert len(alerts) == 1
    assert alerts[0].reason == "repair_exhausted"
    assert alerts[0].stage == "repair"


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
            state = await _await_human_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_HUMAN.value
            assert state["stage"] == "repair"
            await handle.signal("submit_repair_decision", args=[False, "release-manager"])
            result = await handle.result()

    assert result.repair_attempts == 3
    assert len(repairs) == 3
    repair_usage = [record for record in usage if record.stage == "REPAIRING"]
    assert len(repair_usage) == 3
    assert all(record.cost_usd == 0.1 for record in repair_usage)
    assert len(alerts) == 1
    assert alerts[0].reason == "cost_budget_exceeded"
    assert alerts[0].stage == "repair"


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
    assert len(alerts) == 1


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
    activities = _make_activities(
        None,
        1,
        commits,
        pr_opens,
        linkbacks,
        ci_result=SUCCESS,
        plan_pause=True,
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


async def test_plan_human_gate_reject_stops_the_run():
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
        plan_pause=True,
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


async def test_plan_rejected_stops_before_implementation():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    impl_requests: list[ImplementationRequest] = []
    alerts: list[AlertHumanInterventionRequest] = []
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
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        approvals=approvals,
        promotions=promotions,
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
        "repo": "archive-massgov-pfml",
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
