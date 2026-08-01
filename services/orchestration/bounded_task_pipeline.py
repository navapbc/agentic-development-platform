"""Bounded-task-to-draft-PR composition threading every write through coordination guards."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from openhands.runtime.caps import COMPLETED
from scripts.config import Settings, get_settings

from services.coordination.claims import WorkClaimStore, WorkUnit
from services.integration.github.mediator import GitHubMediator
from services.integration.jira.edits import PauseSignal, recheck_issue_before_pr
from services.integration.jira.linkback import LinkbackClient, post_pr_linkback
from services.integration.jira.snapshot import TaskSnapshotStore, capture_task_snapshot
from services.orchestration.branch_sync import CONFLICTS, BranchSynchronizer
from services.orchestration.materialization import MaterializedWorkspace, WorkspaceMaterializer

CLAIM_DENIED = "claim_denied"
IMPL_CAP_BREACH = "impl_cap_breach"
SYNC_PAUSED = "sync_paused"
RECHECK_PAUSED = "recheck_paused"
NO_CHANGES = "no_changes"
DRAFT_PR_OPEN = "draft_pr_open"
EXISTING_PR_REUSED = "existing_pr_reused"

_ENSURE_BRANCH_SEQUENCE = 1
_COMMIT_SEQUENCE = 2
_OPEN_PR_SEQUENCE = 3


@dataclass(frozen=True)
class CodeResult:
    cost: float
    tokens: int
    llm_calls: int
    terminal_reason: str
    summary: str = ""


class CodeStep(Protocol):
    async def run(self, *, workspace: MaterializedWorkspace, task: str) -> CodeResult: ...


@dataclass
class PipelineDeps:
    snapshot_store: TaskSnapshotStore
    claim_store: WorkClaimStore
    materializer: WorkspaceMaterializer
    code_step: CodeStep
    synchronizer: BranchSynchronizer
    mediator: GitHubMediator
    jira_reader: Any
    linkback_client: LinkbackClient
    settings: Settings | None = None


@dataclass(frozen=True)
class PipelineRequest:
    task_id: str
    execution_id: str
    repo: str
    base: str
    base_sha: str
    task: str
    issue_key: str
    raw_issue: Mapping[str, Any]
    jira_source: str
    work_unit: str = "unit-1"
    agent_id: str = "agent-a"
    file_impact: tuple[str, ...] = ()
    commit_message: str = "Apply bounded agent change"
    max_cost_usd: float = 5.0
    max_llm_calls: int = 200
    max_tokens: int = 2_000_000


@dataclass(frozen=True)
class PipelineResult:
    execution_id: str
    status: str
    snapshot_id: str = ""
    branch: str = ""
    terminal_reason: str = ""
    pr_number: int = 0
    pr_url: str = ""
    linkback: str = ""
    sequence_used: int = 0


def _breached(result: CodeResult, request: PipelineRequest) -> bool:
    over_budget = (
        result.cost > request.max_cost_usd
        or result.tokens > request.max_tokens
        or result.llm_calls > request.max_llm_calls
    )
    return over_budget or result.terminal_reason != COMPLETED


async def run_bounded_task(
    deps: PipelineDeps,
    request: PipelineRequest,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    def _progress(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    settings = deps.settings or get_settings()
    _progress("intake: capturing task snapshot")
    snapshot = await capture_task_snapshot(
        dict(request.raw_issue),
        execution_id=request.execution_id,
        jira_source=request.jira_source,
        settings=settings,
        store=deps.snapshot_store,
    )

    unit = WorkUnit(
        request.task_id,
        request.work_unit,
        request.execution_id,
        request.agent_id,
        request.file_impact,
    )
    _progress("claim: acquiring work claim")
    claim = await deps.claim_store.try_claim(unit)
    if not claim.won:
        return PipelineResult(request.execution_id, CLAIM_DENIED, snapshot_id=snapshot.snapshot_id)

    _progress("pickup: checking for existing draft PR")
    existing_pr = await deps.mediator.find_open_pr(request.repo, request.task_id)
    if existing_pr is not None:
        _progress("pickup: reusing existing draft PR")
        reuse_linkback = post_pr_linkback(
            deps.linkback_client,
            request.issue_key,
            execution_id=request.execution_id,
            pr_url=existing_pr.url,
            pr_title=existing_pr.title,
            repo=request.repo,
            branch=existing_pr.head,
        )
        return PipelineResult(
            request.execution_id,
            EXISTING_PR_REUSED,
            snapshot_id=snapshot.snapshot_id,
            branch=existing_pr.head,
            pr_number=existing_pr.number,
            pr_url=existing_pr.url,
            linkback=reuse_linkback.action,
        )

    _progress("branch: ensuring agent branch")
    branch_ref = await deps.mediator.ensure_agent_branch(
        request.repo,
        request.task_id,
        request.execution_id,
        request.base,
        sequence=_ENSURE_BRANCH_SEQUENCE,
    )

    _progress("materialize: preparing workspace")
    workspace = await deps.materializer.materialize(
        repo=request.repo,
        task_id=request.task_id,
        execution_id=request.execution_id,
        base=request.base,
        commit_sha=request.base_sha,
    )

    _progress("code: agent authoring change")
    code = await deps.code_step.run(workspace=workspace, task=request.task)
    if _breached(code, request):
        return PipelineResult(
            request.execution_id,
            IMPL_CAP_BREACH,
            snapshot_id=snapshot.snapshot_id,
            branch=branch_ref.name,
            terminal_reason=code.terminal_reason,
        )

    _progress("sync: rebasing branch onto base")
    sync = await deps.synchronizer.synchronize(
        repo=request.repo,
        base=request.base,
        base_sha=request.base_sha,
        branch=workspace.branch,
        workspace_path=workspace.path,
        resolved_files={},
    )
    if sync.human_commits or sync.status == CONFLICTS:
        return PipelineResult(
            request.execution_id,
            SYNC_PAUSED,
            snapshot_id=snapshot.snapshot_id,
            branch=workspace.branch,
            terminal_reason=sync.status,
        )

    try:
        _progress("recheck: verifying issue is still actionable")
        await recheck_issue_before_pr(
            deps.jira_reader,
            request.issue_key,
            dict(snapshot.payload),
            execution_id=request.execution_id,
            task_id=request.task_id,
            settings=settings,
        )
    except PauseSignal as pause:
        return PipelineResult(
            request.execution_id,
            RECHECK_PAUSED,
            snapshot_id=snapshot.snapshot_id,
            branch=workspace.branch,
            terminal_reason=pause.classification.category,
        )

    commit_request = await deps.materializer.build_commit_request(
        workspace, message=request.commit_message
    )
    if not commit_request.files:
        return PipelineResult(
            request.execution_id,
            NO_CHANGES,
            snapshot_id=snapshot.snapshot_id,
            branch=workspace.branch,
            terminal_reason="agent produced no file changes",
        )
    _progress("commit: pushing branch")
    await deps.mediator.commit_and_push(commit_request, sequence=_COMMIT_SEQUENCE)

    title = f"[{request.task_id}] {snapshot.payload.get('summary') or request.task}"
    body = f"Automated draft for {request.issue_key} (execution {request.execution_id})."
    _progress("pr: opening draft pull request")
    pull_request = await deps.mediator.open_draft_pr(
        request.repo,
        request.task_id,
        request.execution_id,
        workspace.branch,
        request.base,
        title,
        body,
        sequence=_OPEN_PR_SEQUENCE,
    )

    _progress("linkback: posting PR link to jira")
    linkback = post_pr_linkback(
        deps.linkback_client,
        request.issue_key,
        execution_id=request.execution_id,
        pr_url=pull_request.url,
        pr_title=title,
        repo=request.repo,
        branch=workspace.branch,
    )

    return PipelineResult(
        request.execution_id,
        DRAFT_PR_OPEN,
        snapshot_id=snapshot.snapshot_id,
        branch=workspace.branch,
        terminal_reason=code.terminal_reason,
        pr_number=pull_request.number,
        pr_url=pull_request.url,
        linkback=linkback.action,
        sequence_used=_OPEN_PR_SEQUENCE,
    )
