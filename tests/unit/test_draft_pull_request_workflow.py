"""Composition test: the draft-PR orchestrator chains implementation, sync, commit, and PR."""

from __future__ import annotations

import uuid

from openhands.runtime.caps import COMPLETED
from services.orchestration.branch_sync import FAST_FORWARD
from services.orchestration.temporal_activities.branch_sync import (
    RecheckRequest,
    RecheckResult,
    ResolveRequest,
    ResolveResult,
    SyncRequest,
    SyncResult,
)
from services.orchestration.temporal_activities.implementation import (
    ClaimRequest,
    ClaimResult,
    ImplementationRequest,
    ImplementationResult,
)
from services.orchestration.temporal_activities.persistence import AuditEvent, ExecutionState
from services.orchestration.temporal_activities.planning import ContextRequest, ContextResult
from services.orchestration.temporal_activities.pull_request import (
    CommitPushRequest,
    CommitPushResult,
    FindOpenPrRequest,
    FindOpenPrResult,
    LinkbackRequest,
    LinkbackResult,
    OpenPrRequest,
    OpenPrResult,
)
from services.orchestration.temporal_workflows.branch_sync import BranchSyncWorkflow
from services.orchestration.temporal_workflows.draft_pull_request import (
    DRAFT_PR_OPEN,
    EXISTING_PR_REUSED,
    DraftPullRequestInput,
    DraftPullRequestWorkflow,
)
from services.orchestration.temporal_workflows.implementation import ImplementationWorkflow
from services.orchestration.temporal_workflows.pull_request import PullRequestWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


async def _run(*, existing_pr: FindOpenPrResult | None = None):
    commits: list[CommitPushRequest] = []
    pr_opens: list[OpenPrRequest] = []
    linkbacks: list[LinkbackRequest] = []

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        return payload.execution_id

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        return event.event_id

    @activity.defn(name="gather_repo_context")
    async def fake_context(req: ContextRequest) -> ContextResult:
        return ContextResult(summary="repo context", package={})

    @activity.defn(name="claim_work_unit")
    async def fake_claim(req: ClaimRequest) -> ClaimResult:
        return ClaimResult(claim_id="claim-1", won=True, claim_version=1)

    @activity.defn(name="find_open_pr")
    async def fake_find_open_pr(req: FindOpenPrRequest) -> FindOpenPrResult:
        return existing_pr or FindOpenPrResult(found=False)

    @activity.defn(name="run_implementation_stage")
    async def fake_stage(req: ImplementationRequest) -> ImplementationResult:
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

    @activity.defn(name="recheck_issue")
    async def fake_recheck(req: RecheckRequest) -> RecheckResult:
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
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=[
                fake_persist,
                fake_audit,
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
            ],
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
    assert commits[0].sequence == 2
    assert len(pr_opens) == 1
    assert pr_opens[0].sequence == 3
    assert len(linkbacks) == 1


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
