"""Temporal activities for draft PR creation and Jira linkback."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from temporalio import activity

log = structlog.get_logger("ticket_write_back")

PLATFORM_ACTOR = "agent-platform"


def _enforce_write_back(
    *, actor: str, backend: str, project: str, issue_key: str, run_id: str, scope: str
) -> None:
    from services.integration.tickets.write_contract import (
        WriteBackContext,
        authorize_write_back,
        load_write_back_scopes,
        write_back_audit,
    )

    ctx = WriteBackContext(
        actor=actor, backend=backend, project=project, issue_key=issue_key, run_id=run_id
    )
    authorize_write_back(
        ctx,
        scope=scope,
        run_owner_issue_key=issue_key,
        allowed_scopes=load_write_back_scopes(),
    )
    log.info(**write_back_audit(ctx, scope=scope, outcome="authorized"))


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
class ParkTaskRequest:
    issue_key: str
    parked_status: str


@dataclass
class ParkTaskResult:
    previous_status: str
    transitioned: bool


@dataclass
class UnparkTaskRequest:
    issue_key: str
    target_status: str


@dataclass
class ApplyStatusRequest:
    issue_key: str
    stage: str
    execution_id: str = ""


@dataclass
class ApplyStatusResult:
    status: str
    transitioned: bool


@dataclass
class ReadStatusRequest:
    issue_key: str


@dataclass
class RecordDependencyLinkRequest:
    dependent_issue_key: str
    blocking_issue_key: str
    execution_id: str = ""
    relationship: str = "depends_on"


@dataclass
class RecordDependencyLinkResult:
    linked: bool
    detail: str = ""


@dataclass
class ReadStatusResult:
    status: str
    is_terminal: bool


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


@dataclass
class PromotePrRequest:
    repo: str
    task_id: str
    execution_id: str
    number: int
    sequence: int


@dataclass
class PromotePrResult:
    number: int
    url: str
    draft: bool
    state: str
    ready: bool


@dataclass
class RecordApprovalRequest:
    repo: str
    task_id: str
    execution_id: str
    number: int
    approved: bool
    approver: str
    sequence: int
    issue_key: str = ""
    project: str = ""


@dataclass
class RecordApprovalResult:
    recorded: bool
    approved: bool
    approver: str


def _build_mediator():
    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.coordination.sequencing import PostgresSequenceGate
    from services.integration.github.factory import build_git_host
    from services.integration.github.mediator import GitHubMediator

    return GitHubMediator.from_settings(
        build_git_host(),
        PostgresSideEffectLedger(),
        PostgresSequenceGate(),
    )


def _build_registry():
    from services.coordination.orphans import PostgresOrphanRegistry

    return PostgresOrphanRegistry()


def _build_ticket_backend(issue_key: str):
    from services.integration.tickets import build_ticket_backend_for_issue

    return build_ticket_backend_for_issue(issue_key)


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
    from services.integration.jira.adapter import project_of
    from services.integration.jira.linkback import post_pr_linkback
    from services.integration.tickets import resolve_ticket_backend_name

    _enforce_write_back(
        actor=PLATFORM_ACTOR,
        backend=resolve_ticket_backend_name(),
        project=project_of(req.issue_key),
        issue_key=req.issue_key,
        run_id=req.execution_id,
        scope="comment",
    )
    adapter = _build_ticket_backend(req.issue_key)
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


@activity.defn
async def mark_task_parked(req: ParkTaskRequest) -> ParkTaskResult:
    adapter = _build_ticket_backend(req.issue_key)
    current = adapter.get_status(req.issue_key)
    if current.strip().lower() == req.parked_status.strip().lower():
        return ParkTaskResult(previous_status=current, transitioned=False)
    adapter.transition(req.issue_key, req.parked_status)
    return ParkTaskResult(previous_status=current, transitioned=True)


@activity.defn
async def unmark_task_parked(req: UnparkTaskRequest) -> str:
    if req.target_status:
        adapter = _build_ticket_backend(req.issue_key)
        adapter.transition(req.issue_key, req.target_status)
    return req.issue_key


@activity.defn
async def apply_task_status(req: ApplyStatusRequest) -> ApplyStatusResult:
    from services.integration.jira.adapter import apply_status, project_of
    from services.integration.tickets import (
        build_ticket_backend_for_issue,
        resolve_status_flow,
    )

    flow = resolve_status_flow(project=project_of(req.issue_key))
    targets = {
        "work_started": flow.work_started,
        "ready_for_review": flow.ready_for_review,
        "terminal": flow.terminal,
    }
    if req.stage not in targets:
        return ApplyStatusResult(status="", transitioned=False)
    target = targets[req.stage]
    if not target:
        return ApplyStatusResult(status="", transitioned=False)
    from services.integration.tickets import resolve_ticket_backend_name

    _enforce_write_back(
        actor=PLATFORM_ACTOR,
        backend=resolve_ticket_backend_name(),
        project=project_of(req.issue_key),
        issue_key=req.issue_key,
        run_id=req.execution_id or req.issue_key,
        scope="status",
    )
    backend = build_ticket_backend_for_issue(req.issue_key)
    transitioned = apply_status(backend, req.issue_key, target)
    return ApplyStatusResult(status=target, transitioned=transitioned)


@activity.defn
async def record_dependency_link(
    req: RecordDependencyLinkRequest,
) -> RecordDependencyLinkResult:
    from services.integration.jira.adapter import project_of
    from services.integration.tickets import DependencyBackend, resolve_ticket_backend_name

    if not req.dependent_issue_key or not req.blocking_issue_key:
        return RecordDependencyLinkResult(linked=False, detail="missing ticket")
    backend = _build_ticket_backend(req.dependent_issue_key)
    if not isinstance(backend, DependencyBackend) or not backend.dependency_graph_active():
        return RecordDependencyLinkResult(
            linked=False, detail="backend has no dependency capability"
        )
    _enforce_write_back(
        actor=PLATFORM_ACTOR,
        backend=resolve_ticket_backend_name(),
        project=project_of(req.dependent_issue_key),
        issue_key=req.dependent_issue_key,
        run_id=req.execution_id or req.dependent_issue_key,
        scope="link",
    )
    backend.link(
        req.dependent_issue_key,
        req.blocking_issue_key,
        relationship=req.relationship,
    )
    return RecordDependencyLinkResult(linked=True, detail=req.blocking_issue_key)


@activity.defn
async def read_ticket_status(req: ReadStatusRequest) -> ReadStatusResult:
    from services.integration.jira.adapter import project_of
    from services.integration.tickets import (
        build_ticket_backend_for_issue,
        resolve_status_flow,
    )

    if not req.issue_key:
        return ReadStatusResult(status="", is_terminal=False)
    backend = build_ticket_backend_for_issue(req.issue_key)
    status = backend.get_status(req.issue_key)
    flow = resolve_status_flow(project=project_of(req.issue_key))
    is_terminal = bool(flow.terminal) and status.strip().lower() == flow.terminal.strip().lower()
    return ReadStatusResult(status=status, is_terminal=is_terminal)


@activity.defn
async def record_pr_approval(req: RecordApprovalRequest) -> RecordApprovalResult:
    mediator = _build_mediator()
    outcome = await mediator.record_promotion_approval(
        req.repo,
        req.task_id,
        req.execution_id,
        req.number,
        approved=req.approved,
        approver=req.approver,
        sequence=req.sequence,
    )
    approved = bool(outcome.get("approved", req.approved))
    approver = str(outcome.get("approver", req.approver))
    if req.issue_key:
        from services.integration.jira.adapter import project_of
        from services.integration.tickets import resolve_ticket_backend_name

        project = req.project or project_of(req.issue_key)
        _enforce_write_back(
            actor=approver or PLATFORM_ACTOR,
            backend=resolve_ticket_backend_name(),
            project=project,
            issue_key=req.issue_key,
            run_id=req.execution_id,
            scope="approval",
        )
        verdict = "approved" if approved else "rejected"
        backend = _build_ticket_backend(req.issue_key)
        actor = approver or PLATFORM_ACTOR
        note = f"Promotion {verdict} by {actor} (execution {req.execution_id})."
        existing = backend.list_comment_bodies(req.issue_key)
        if not any(note in body for body in existing):
            backend.add_comment(req.issue_key, note)
    return RecordApprovalResult(
        recorded=True,
        approved=approved,
        approver=approver,
    )


@activity.defn
async def promote_pr(req: PromotePrRequest) -> PromotePrResult:
    mediator = _build_mediator()
    pr = await mediator.mark_pr_ready(
        req.repo,
        req.task_id,
        req.execution_id,
        req.number,
        sequence=req.sequence,
    )
    return PromotePrResult(
        number=pr.number,
        url=pr.url,
        draft=pr.draft,
        state=pr.state,
        ready=not pr.draft,
    )
