"""Composition test: the draft-PR orchestrator chains implementation, sync, commit, and PR."""

from __future__ import annotations

import asyncio
import uuid

from adapters.interfaces.ci_host import COMPLETED as CI_COMPLETED
from adapters.interfaces.ci_host import FAILURE, RUNNING, SUCCESS
from adapters.interfaces.ci_host import UNAVAILABLE as CI_UNAVAILABLE
from openhands.runtime.caps import COMPLETED
from services.orchestration.branch_sync import FAST_FORWARD
from services.orchestration.suspension import (
    RESUME_SIGNAL,
    ResumeOn,
    SuspendReason,
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
from services.orchestration.temporal_activities.ci import (
    PollCIRequest,
    PollCIResult,
    ReadCILogRequest,
    ReadCILogResult,
    TriggerCIRequest,
    TriggerCIResult,
)
from services.orchestration.temporal_activities.implementation import (
    ClaimRequest,
    ClaimResult,
    ImplementationRequest,
    ImplementationResult,
)
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
    RecordApprovalRequest,
    RecordApprovalResult,
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
    CI_FAILED,
    CI_PASSED,
    DRAFT_PR_OPEN,
    EXISTING_PR_REUSED,
    PLAN_REJECTED,
    POLICY_ALLOWED,
    POLICY_DENIED,
    PR_PROMOTED,
    PROMOTION_CANCELLED,
    REVIEW_BLOCKED,
    REVIEW_PASSED,
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
    ci_triggers: list[TriggerCIRequest] | None = None,
    ci_result: str = SUCCESS,
    ci_results: list[str] | None = None,
    ci_running_polls: int = 0,
    ci_unavailable_polls: int = 0,
    repairs: list[ApplyCIRepairRequest] | None = None,
    repair_actionable: bool = True,
    alerts: list[AlertHumanInterventionRequest] | None = None,
    usage: list[UsageMetrics] | None = None,
    review_requests: list[ReviewRequest] | None = None,
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
    policy_requests: list[EvaluatePromotionRequest] | None = None,
    policy_allow: bool = True,
    policy_reasons: list[str] | None = None,
    approvals: list[RecordApprovalRequest] | None = None,
    promotions: list[PromotePrRequest] | None = None,
    recheck_requires_pause: bool = False,
    recheck_category: str = "",
):
    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        return payload.execution_id

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
        return CommitPushResult(
            branch="agent/T-1", commit_sha="c" * 40, pushed=True, replayed=False
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

    trigger_count = {"n": 0}
    build_result: dict[int, str] = {}

    @activity.defn(name="trigger_ci")
    async def fake_trigger_ci(req: TriggerCIRequest) -> TriggerCIResult:
        idx = trigger_count["n"]
        trigger_count["n"] += 1
        build_number = 7 + idx
        if ci_results is not None:
            build_result[build_number] = ci_results[min(idx, len(ci_results) - 1)]
        else:
            build_result[build_number] = ci_result
        if ci_triggers is not None:
            ci_triggers.append(req)
        return TriggerCIResult(
            build_number=build_number,
            queue_id=42,
            url=f"https://ci.example/{build_number}",
            triggered=True,
        )

    poll_counts: dict[int, int] = {}

    @activity.defn(name="poll_ci")
    async def fake_poll_ci(req: PollCIRequest) -> PollCIResult:
        n = poll_counts.get(req.build_number, 0) + 1
        poll_counts[req.build_number] = n
        url = f"https://ci.example/{req.build_number}"
        if n <= ci_unavailable_polls:
            return PollCIResult(status=CI_UNAVAILABLE, result="", url="", completed=False)
        if n <= ci_unavailable_polls + ci_running_polls:
            return PollCIResult(status=RUNNING, result="", url=url, completed=False)
        return PollCIResult(
            status=CI_COMPLETED,
            result=build_result.get(req.build_number, ci_result),
            url=url,
            completed=True,
        )

    @activity.defn(name="read_ci_log")
    async def fake_read_ci_log(req: ReadCILogRequest) -> ReadCILogResult:
        return ReadCILogResult(
            failure_summary="AssertionError: expected 1 got 2",
            text="FAILED tests/test_x.py::test_x - AssertionError",
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

    @activity.defn(name="review_code")
    async def fake_review_code(req: ReviewRequest) -> ReviewOutcome:
        if review_requests is not None:
            review_requests.append(req)
        return _review_outcome("code-review", code_verdict)

    @activity.defn(name="review_security")
    async def fake_review_security(req: ReviewRequest) -> ReviewOutcome:
        if review_requests is not None:
            review_requests.append(req)
        return _review_outcome("security-review", security_verdict)

    @activity.defn(name="verify_acceptance")
    async def fake_verify_acceptance(req: ReviewRequest) -> ReviewOutcome:
        if review_requests is not None:
            review_requests.append(req)
        return _review_outcome("acceptance-verification", acceptance_verdict)

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
        return ScanChangeResult(
            scanners=["semgrep", "trivy"],
            findings=list(scan_result_findings or []),
            blocking_count=scan_blocking_findings,
            threshold="high",
            content_hash="sha256:scan",
            evidence_id="evidence-1",
            severity_counts=dict(scan_result_severity_counts or {}),
        )

    @activity.defn(name="evaluate_promotion")
    async def fake_evaluate_promotion(req: EvaluatePromotionRequest) -> EvaluatePromotionResult:
        if policy_requests is not None:
            policy_requests.append(req)
        return EvaluatePromotionResult(
            allow=policy_allow,
            bundle_version="0.2.0",
            next_action="proceed" if policy_allow else "block_and_report",
            reasons=list(policy_reasons or []),
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

    return [
        fake_persist,
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
        fake_trigger_ci,
        fake_poll_ci,
        fake_read_ci_log,
        fake_analyze,
        fake_apply_repair,
        fake_alert,
        fake_review_code,
        fake_review_security,
        fake_verify_acceptance,
        fake_invoke_model,
        fake_review_plan,
        fake_scan_change,
        fake_evaluate_promotion,
        fake_record_approval,
        fake_promote,
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

    assert result.status == DRAFT_PR_OPEN
    assert result.branch == "agent/T-1"
    assert result.pr_number == 5
    assert result.pr_url == "https://github.com/org/agentic-demo-service/pull/5"
    assert result.linkback == "applied"
    assert len(commits) == 1
    assert commits[0].sequence == 12
    assert len(pr_opens) == 1
    assert pr_opens[0].sequence == 13
    assert len(linkbacks) == 1


async def test_reclaimed_execution_uses_higher_gate_sequences():
    result, commits, pr_opens, _ = await _run(claim_version=2)

    assert result.status == DRAFT_PR_OPEN
    assert commits[0].sequence == 22
    assert pr_opens[0].sequence == 23


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
            assert state["stage"] == "finalize"
            assert commits == []

            await handle.signal(RESUME_SIGNAL, ResumeOn.NEWER_BUILD.value)
            result = await handle.result()

    assert result.status == DRAFT_PR_OPEN
    assert result.pr_number == 5
    assert len(commits) == 1
    assert commits[0].sequence == 12
    assert len(pr_opens) == 1


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

    assert result.status == DRAFT_PR_OPEN
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
        ci_job="agentic-demo-service-ci",
        ci_poll_interval_seconds=poll_interval,
        ci_poll_max_attempts=5,
        ci_unavailable_alert_threshold=unavailable_threshold,
        max_repair_attempts=max_repair_attempts,
        max_cost_usd=max_cost_usd,
        run_reviews=run_reviews,
        run_policy=run_policy,
        run_approval=run_approval,
        run_planning=run_planning,
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
    ci_triggers: list[TriggerCIRequest] = []
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

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"
    assert result.ci_build_number == 7
    assert result.ci_build_id == "7"
    assert len(ci_triggers) == 1
    assert ci_triggers[0].job == "agentic-demo-service-ci"
    assert ci_triggers[0].branch == "agent/T-1"
    assert ci_triggers[0].commit_sha == "c" * 40
    assert ci_triggers[0].sequence == 14


async def test_ci_webhook_signal_resumes_parked_wait_to_pass():
    activities = _make_activities(
        None, 1, [], [], [], ci_result=SUCCESS, ci_running_polls=1
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(poll_interval=0.0),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
            state = await _await_ci_park(handle)
            assert state["reason"] == SuspendReason.AWAITING_CI.value
            assert state["resume_on"] == ResumeOn.CI_RESULT.value
            assert state["stage"] == "ci"
            await handle.signal(DraftPullRequestWorkflow.submit_ci_result)
            result = await handle.result()

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"


async def test_ci_poll_fallback_resumes_without_a_webhook():
    activities = _make_activities(
        None, 1, [], [], [], ci_result=SUCCESS, ci_running_polls=1
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(poll_interval=1.0),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"


async def test_ci_unavailable_parks_alerts_then_resumes_when_ci_returns():
    alerts: list[AlertHumanInterventionRequest] = []
    activities = _make_activities(
        None,
        1,
        [],
        [],
        [],
        ci_result=SUCCESS,
        ci_unavailable_polls=2,
        alerts=alerts,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(poll_interval=1.0, unavailable_threshold=2),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"
    assert len(alerts) == 1
    assert "unreachable" in alerts[0].detail
    assert alerts[0].reason == "ci_unavailable"
    assert alerts[0].stage == "ci"


async def test_ci_failure_repairs_and_passes():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list[TriggerCIRequest] = []
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

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"
    assert result.repair_attempts == 1
    assert result.ci_build_number == 8
    assert len(repairs) == 1
    assert repairs[0].instruction == "Fix the failing assertion"
    assert len(ci_triggers) == 2
    assert ci_triggers[1].sequence == 16
    assert len(commits) == 2
    assert commits[1].sequence == 15


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
    ci_triggers: list[TriggerCIRequest] = []
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
    assert ci_triggers[2].sequence == 18
    assert len(alerts) == 1
    assert alerts[0].reason == "repair_exhausted"
    assert alerts[0].stage == "repair"


async def test_ci_repair_stops_on_cumulative_budget():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list[TriggerCIRequest] = []
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
    ci_triggers: list[TriggerCIRequest] = []
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

    assert result.status == CI_PASSED
    assert result.repair_attempts == 2
    assert len(repairs) == 1
    assert len(ci_triggers) == 3
    assert len(alerts) == 1


async def test_ci_failure_unactionable_parks_for_human():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    ci_triggers: list[TriggerCIRequest] = []
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
    assert result.ci_build_number == 7
    assert len(repairs) == 0
    assert len(ci_triggers) == 1
    assert len(alerts) == 1


async def test_ci_parks_on_awaiting_ci_and_resumes_on_build_result():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(
        None, 1, commits, pr_opens, linkbacks, ci_result=SUCCESS, ci_running_polls=1
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            handle = await env.client.start_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(poll_interval=0.0),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            state: dict[str, str] = {}
            for _ in range(50):
                state = await handle.query(DraftPullRequestWorkflow.suspension_state)
                if state.get("reason") == SuspendReason.AWAITING_CI.value:
                    break
                await asyncio.sleep(0.05)

            assert state["reason"] == SuspendReason.AWAITING_CI.value
            assert state["resume_on"] == ResumeOn.CI_RESULT.value
            assert state["parked_build_id"] == "7"
            assert state["stage"] == "ci"

            await handle.signal(RESUME_SIGNAL, ResumeOn.CI_RESULT.value)
            result = await handle.result()

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"
    assert result.ci_build_number == 7


async def test_ci_bounded_poll_resumes_without_a_signal():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    activities = _make_activities(
        None, 1, commits, pr_opens, linkbacks, ci_result=SUCCESS, ci_running_polls=1
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"draft-{uuid.uuid4().hex[:8]}"
        async with await _start_ci_worker(env, task_queue, activities):
            result = await env.client.execute_workflow(
                DraftPullRequestWorkflow.run,
                _ci_input(poll_interval=30.0),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == CI_PASSED
    assert result.ci_status == "passed"
    assert result.ci_build_number == 7


async def test_ci_pass_flows_through_review_gates_with_attestations():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    review_requests: list[ReviewRequest] = []
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

    assert result.status == REVIEW_PASSED
    assert result.review_status == "passed"
    assert result.code_verdict == "pass"
    assert result.security_verdict == "pass"
    assert result.acceptance_verdict == "pass"
    assert result.review_attestations == {
        "code-review": "sha256:code-review",
        "security-review": "sha256:security-review",
        "acceptance-verification": "sha256:acceptance-verification",
    }
    assert len(review_requests) == 3
    assert review_requests[0].ref == "c" * 40
    assert review_requests[0].repo_root == "/tmp/ws/agent-T-1"


async def test_review_blocking_finding_stops_before_policy_gate():
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []
    review_requests: list[ReviewRequest] = []
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
    assert result.acceptance_verdict == ""
    assert result.review_attestations == {
        "code-review": "sha256:code-review",
        "security-review": "sha256:security-review",
    }
    assert result.review_findings == {
        "code-review": [],
        "security-review": ["security-review blocking finding"],
    }
    assert result.review_summaries == {
        "code-review": "reviewed",
        "security-review": "reviewed",
    }
    assert len(review_requests) == 2
    assert len(alerts) == 1
    assert alerts[0].reason == REVIEW_BLOCKED
    assert alerts[0].stage == "review"


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

    assert result.status == REVIEW_PASSED
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
    review_requests: list[ReviewRequest] = []
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

    assert result.status == REVIEW_PASSED
    assert "done" in pr_opens[0].body
    assert any(
        "Implementation summary:" in request.subject and "done" in request.subject
        for request in review_requests
    )
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

    assert result.status == POLICY_ALLOWED
    assert result.policy_status == "allowed"
    assert result.policy_bundle_version == "0.2.0"
    assert result.policy_next_action == "proceed"
    assert result.policy_reasons == []
    assert result.scan_blocking_findings == 0
    assert len(scan_requests) == 1
    assert scan_requests[0].ref == "c" * 40
    assert scan_requests[0].repo_root == "/tmp/ws/agent-T-1"
    assert len(policy_requests) == 1
    assert policy_requests[0].jira_project == "AGENTDEV"
    assert policy_requests[0].ci_status == "success"
    assert policy_requests[0].plan_approved is True
    assert policy_requests[0].acceptance_verified is True
    assert policy_requests[0].pr_authorized is True


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
        policy_reasons=["blocking_security_findings"],
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
    assert result.policy_reasons == ["blocking_security_findings"]
    assert result.scan_blocking_findings == 1
    assert policy_requests[0].blocking_findings == 1
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
        policy_reasons=["blocking_security_findings"],
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
    assert policy_requests[0].blocking_findings == 1
    assert policy_requests[0].severity_counts == {"high": 1, "medium": 1}
    assert [finding["rule_id"] for finding in policy_requests[0].scan_findings] == [
        "python.sql-injection",
        "CVE-2024-0001",
    ]


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
    assert approvals[0].sequence == 25
    assert len(promotions) == 1
    assert promotions[0].sequence == 26
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
