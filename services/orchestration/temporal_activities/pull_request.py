"""Temporal activities for draft PR creation and Jira linkback."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity


@dataclass
class OpenPrRequest:
    repo: str
    task_id: str
    execution_id: str
    head: str
    base: str
    title: str
    body: str
    sequence: int
    workflow_id: str = ""


@dataclass
class OpenPrResult:
    number: int
    url: str
    created: bool
    draft: bool
    state: str
    orphan_id: str


@dataclass
class LinkbackRequest:
    issue_key: str
    execution_id: str
    pr_url: str
    pr_title: str
    repo: str
    branch: str


@dataclass
class LinkbackResult:
    action: str
    detail: str


@dataclass
class FindOpenPrRequest:
    repo: str
    task_id: str


@dataclass
class FindOpenPrResult:
    found: bool
    number: int = 0
    url: str = ""
    title: str = ""
    head: str = ""


@dataclass
class CommitPushRequest:
    repo: str
    task_id: str
    execution_id: str
    base: str
    commit: str
    message: str
    sequence: int = 2


@dataclass
class CommitPushResult:
    branch: str
    commit_sha: str
    pushed: bool
    replayed: bool


def _build_mediator():
    from adapters.real.git_host import RealGitHost

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.coordination.sequencing import PostgresSequenceGate
    from services.integration.github.mediator import GitHubMediator

    return GitHubMediator.from_settings(
        RealGitHost.from_settings(),
        PostgresSideEffectLedger(),
        PostgresSequenceGate(),
    )


def _build_registry():
    from services.coordination.orphans import PostgresOrphanRegistry

    return PostgresOrphanRegistry()


def _build_jira_adapter():
    from services.integration.jira.adapter import build_jira_adapter

    return build_jira_adapter()


@activity.defn
async def create_draft_pr(req: OpenPrRequest) -> OpenPrResult:
    from services.coordination.orphans import PR, OrphanArtifact

    mediator = _build_mediator()
    pr = await mediator.open_draft_pr(
        req.repo,
        req.task_id,
        req.execution_id,
        req.head,
        req.base,
        req.title,
        req.body,
        sequence=req.sequence,
    )
    registry = _build_registry()
    orphan_id = await registry.register(
        OrphanArtifact(
            artifact_type=PR,
            external_id=f"{req.repo}#{pr.number}",
            repository=req.repo,
            owning_workflow=req.workflow_id,
        )
    )
    return OpenPrResult(
        number=pr.number,
        url=pr.url,
        created=pr.created,
        draft=pr.draft,
        state=pr.state,
        orphan_id=orphan_id,
    )


@activity.defn
async def find_open_pr(req: FindOpenPrRequest) -> FindOpenPrResult:
    mediator = _build_mediator()
    pr = await mediator.find_open_pr(req.repo, req.task_id)
    if pr is None:
        return FindOpenPrResult(found=False)
    return FindOpenPrResult(
        found=True,
        number=pr.number,
        url=pr.url,
        title=pr.title,
        head=pr.head,
    )


@activity.defn
async def commit_and_push(req: CommitPushRequest) -> CommitPushResult:
    from services.coordination.orphans import PostgresOrphanRegistry
    from services.orchestration.materialization import WorkspaceMaterializer

    materializer = WorkspaceMaterializer.from_settings(PostgresOrphanRegistry())
    workspace = await materializer.materialize(
        repo=req.repo,
        task_id=req.task_id,
        execution_id=req.execution_id,
        base=req.base,
        commit_sha=req.commit,
    )
    commit_request = await materializer.build_commit_request(workspace, message=req.message)
    mediator = _build_mediator()
    result = await mediator.commit_and_push(commit_request, sequence=req.sequence)
    return CommitPushResult(
        branch=result.branch,
        commit_sha=result.commit_sha,
        pushed=result.pushed,
        replayed=result.replayed,
    )


@activity.defn
async def link_pr_to_issue(req: LinkbackRequest) -> LinkbackResult:
    from services.integration.jira.linkback import post_pr_linkback

    adapter = _build_jira_adapter()
    result = post_pr_linkback(
        adapter,
        req.issue_key,
        execution_id=req.execution_id,
        pr_url=req.pr_url,
        pr_title=req.pr_title,
        repo=req.repo,
        branch=req.branch,
    )
    return LinkbackResult(action=result.action, detail=result.detail)
