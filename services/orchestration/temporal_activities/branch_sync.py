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


def _build_synchronizer():
    from services.orchestration.branch_sync import GitBranchSynchronizer

    return GitBranchSynchronizer.from_settings()


@activity.defn
async def synchronize_branch(req: SyncRequest) -> SyncResult:
    synchronizer = _build_synchronizer()
    outcome: SyncOutcome = await synchronizer.synchronize(
        repo=req.repo,
        base=req.base,
        base_sha=req.base_sha,
        branch=req.branch,
        workspace_path=req.workspace_path,
        resolved_files=req.resolved_files,
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
    from services.integration.jira.adapter import build_jira_adapter_for_issue
    from services.integration.jira.edits import PauseSignal, recheck_issue_before_pr

    reader = build_jira_adapter_for_issue(req.issue_key)
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
