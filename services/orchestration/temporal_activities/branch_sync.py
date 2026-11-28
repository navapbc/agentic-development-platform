"""Temporal activities for the branch-synchronization and conflict-resolution stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from services.orchestration.branch_sync import ConflictPayload, SyncOutcome


@dataclass
class SyncRequest:
    repo: str
    task_id: str
    execution_id: str
    base: str
    base_sha: str
    branch: str
    workspace_path: str
    resolved_files: dict[str, str] = field(default_factory=dict)


@dataclass
class SyncResult:
    status: str
    base_head: str
    human_commits: bool
    conflicts: list[ConflictPayload] = field(default_factory=list)


@dataclass
class ResolveRequest:
    conflicts: list[ConflictPayload]
    cap: int


@dataclass
class ResolveResult:
    status: str
    resolved: dict[str, str] = field(default_factory=dict)
    material: list[str] = field(default_factory=list)


@dataclass
class RecheckRequest:
    issue_key: str
    execution_id: str
    task_id: str
    stored_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecheckResult:
    category: str
    requires_pause: bool
    changed_fields: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ResyncRequest:
    repo: str
    task_id: str
    execution_id: str
    base: str
    commit: str


@dataclass
class ResyncResult:
    status: str
    base_head: str
    conflicts: list[str] = field(default_factory=list)


def _build_synchronizer():
    from services.orchestration.branch_sync import GitBranchSynchronizer

    return GitBranchSynchronizer.from_settings()


def _lockfiles(checks) -> frozenset[str]:
    """The lockfiles the dependency step regenerates, which a base change cannot conflict with."""
    return frozenset(checks.lockfiles if checks is not None and checks.lock_command else ())


@activity.defn
async def resync_branch(req: ResyncRequest) -> ResyncResult:
    """Bring the committed agent branch onto the current base head in the run's workspace.

    The workspace returns to the branch head first, so a retried attempt starts from the commit
    rather than from a merge an earlier attempt left behind.
    """
    from scripts.config import get_settings

    from services.coordination.orphans import PostgresOrphanRegistry
    from services.orchestration.checks.config import checks_for_repo
    from services.orchestration.materialization import WorkspaceMaterializer

    materializer = WorkspaceMaterializer.from_settings(PostgresOrphanRegistry())
    workspace = await materializer.materialize(
        repo=req.repo,
        task_id=req.task_id,
        execution_id=req.execution_id,
        base=req.base,
        commit_sha=req.commit,
    )
    await materializer.discard_changes(workspace)
    outcome = await _build_synchronizer().resync(
        repo=req.repo,
        base=req.base,
        workspace_path=workspace.path,
        regenerated=_lockfiles(checks_for_repo(req.repo, get_settings())),
    )
    return ResyncResult(
        status=outcome.status, base_head=outcome.base_head, conflicts=list(outcome.conflicts)
    )


@activity.defn
async def synchronize_branch(req: SyncRequest) -> SyncResult:
    from scripts.config import get_settings

    from services.orchestration.checks.config import checks_for_repo

    synchronizer = _build_synchronizer()
    checks = checks_for_repo(req.repo, get_settings())
    outcome: SyncOutcome = await synchronizer.synchronize(
        repo=req.repo,
        base=req.base,
        base_sha=req.base_sha,
        branch=req.branch,
        workspace_path=req.workspace_path,
        resolved_files=req.resolved_files,
        regenerated=_lockfiles(checks),
    )
    return SyncResult(
        status=outcome.status,
        base_head=outcome.base_head,
        human_commits=outcome.human_commits,
        conflicts=list(outcome.conflicts),
    )


@activity.defn
async def resolve_conflicts(req: ResolveRequest) -> ResolveResult:
    from services.orchestration.langgraph.conflict_resolution import (
        Conflict,
    )
    from services.orchestration.langgraph.conflict_resolution import (
        resolve_conflicts as run_resolution,
    )

    conflicts = [
        Conflict(path=c.path, ours=c.ours, theirs=c.theirs, base=c.base) for c in req.conflicts
    ]
    resolution = run_resolution(conflicts, cap=req.cap)
    return ResolveResult(
        status=resolution.status,
        resolved=resolution.resolved,
        material=resolution.material,
    )


@activity.defn
async def recheck_issue(req: RecheckRequest) -> RecheckResult:
    from services.integration.jira.edits import PauseSignal, recheck_issue_before_pr
    from services.integration.tickets import build_ticket_backend_for_issue

    reader = build_ticket_backend_for_issue(req.issue_key)
    try:
        classification = await recheck_issue_before_pr(
            reader,
            req.issue_key,
            req.stored_payload,
            execution_id=req.execution_id,
            task_id=req.task_id,
        )
    except PauseSignal as signal:
        classification = signal.classification
    return RecheckResult(
        category=classification.category,
        requires_pause=classification.requires_pause,
        changed_fields=list(classification.changed_fields),
        reason=classification.reason,
    )
