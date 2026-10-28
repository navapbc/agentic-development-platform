"""Temporal workflow for the branch-synchronization and conflict-resolution stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from services.orchestration.branch_sync import FAST_FORWARD, REBASED
    from services.orchestration.temporal_activities.branch_sync import (
        RecheckRequest,
        RecheckResult,
        ResolveRequest,
        ResolveResult,
        SyncRequest,
        SyncResult,
        recheck_issue,
        resolve_conflicts,
        synchronize_branch,
    )
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        ExecutionState,
        persist_execution_state,
        record_audit_event,
    )

REBASE_CAP = 2
RESOLUTION_CAP = 2
RESOLUTION_PAUSED = "paused"

HUMAN_COMMITS = "human_commits"
REBASE_CAP_EXCEEDED = "rebase_cap_exceeded"
MATERIAL_CONFLICT = "material_conflict"


@dataclass
class BranchSyncInput:
    task_id: str
    execution_id: str
    repo: str
    base: str
    base_sha: str
    branch: str
    workspace_path: str
    issue_key: str = ""
    stored_issue: dict[str, Any] = field(default_factory=dict)
    rebase_cap: int = REBASE_CAP
    resolution_cap: int = RESOLUTION_CAP


@dataclass
class BranchSyncOutput:
    execution_id: str
    status: str
    terminal_reason: str
    base_head: str = ""
    rebases: int = 0
    drift: bool = False
    escalated: bool = False
    detail: str = ""


@workflow.defn
class BranchSyncWorkflow:
    @workflow.run
    async def run(self, params: BranchSyncInput) -> BranchSyncOutput:
        retry = RetryPolicy(maximum_attempts=3)
        short = timedelta(seconds=30)
        sync_timeout = timedelta(minutes=5)
        workflow_id = workflow.info().workflow_id

        async def persist(state: str) -> None:
            await workflow.execute_activity(
                persist_execution_state,
                ExecutionState(
                    execution_id=params.execution_id,
                    task_id=params.task_id,
                    state=state,
                    workflow_id=workflow_id,
                    repository=params.repo or None,
                    repository_sha=params.base_sha or None,
                ),
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        async def audit(event: AuditEvent) -> None:
            await workflow.execute_activity(
                record_audit_event,
                event,
                start_to_close_timeout=short,
                retry_policy=retry,
            )

        def pause_output(reason: str, detail: str = "") -> BranchSyncOutput:
            return BranchSyncOutput(
                execution_id=params.execution_id,
                status="paused",
                terminal_reason=reason,
                base_head=base_head,
                rebases=rebases,
                drift=drift,
                escalated=True,
                detail=detail,
            )

        await persist("IMPLEMENTING")

        resolved_files: dict[str, str] = {}
        rebases = 0
        base_head = ""
        drift = False
        drift_audited = False

        while True:
            sync: SyncResult = await workflow.execute_activity(
                synchronize_branch,
                SyncRequest(
                    repo=params.repo,
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    base=params.base,
                    base_sha=params.base_sha,
                    branch=params.branch,
                    workspace_path=params.workspace_path,
                    resolved_files=resolved_files,
                ),
                start_to_close_timeout=sync_timeout,
                retry_policy=retry,
            )
            base_head = sync.base_head

            if sync.status != FAST_FORWARD and not drift_audited:
                drift = True
                drift_audited = True
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:BRANCH_DRIFT_DETECTED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="BRANCH_DRIFT_DETECTED",
                        stage="BRANCH_SYNC",
                        new_state="IMPLEMENTING",
                        decision="drift",
                        reason=f"base {params.base} advanced to {base_head}",
                        workflow_id=workflow_id,
                    )
                )

            if sync.human_commits:
                human_commits_detail = "human commits present on the agent branch"
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:BRANCH_SYNC_PAUSED:human",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="BRANCH_SYNC_PAUSED",
                        stage="BRANCH_SYNC",
                        new_state="paused",
                        decision="pause",
                        reason=human_commits_detail,
                        workflow_id=workflow_id,
                    )
                )
                return pause_output(HUMAN_COMMITS, human_commits_detail)

            if sync.status in (FAST_FORWARD, REBASED):
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:BRANCH_SYNCED:{rebases}",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="BRANCH_SYNCED",
                        stage="BRANCH_SYNC",
                        new_state="IMPLEMENTING",
                        decision=sync.status,
                        reason=f"agent branch aligned with {base_head}",
                        workflow_id=workflow_id,
                    )
                )
                clean_reason = sync.status
                break

            if rebases >= params.rebase_cap:
                cap_detail = (
                    f"unresolved conflicts after {rebases} rebases (cap {params.rebase_cap})"
                )
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:BRANCH_SYNC_PAUSED:cap",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="BRANCH_SYNC_PAUSED",
                        stage="BRANCH_SYNC",
                        new_state="paused",
                        decision="pause",
                        reason=cap_detail,
                        workflow_id=workflow_id,
                    )
                )
                return pause_output(REBASE_CAP_EXCEEDED, cap_detail)

            rebases += 1
            resolution: ResolveResult = await workflow.execute_activity(
                resolve_conflicts,
                ResolveRequest(conflicts=list(sync.conflicts), cap=params.resolution_cap),
                start_to_close_timeout=sync_timeout,
                retry_policy=retry,
            )

            if resolution.status == RESOLUTION_PAUSED:
                material_detail = f"material conflicts require a human: {resolution.material}"
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:CONFLICT_PAUSE:{rebases}",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="CONFLICT_PAUSE",
                        stage="BRANCH_SYNC",
                        new_state="paused",
                        decision="pause",
                        reason=material_detail,
                        workflow_id=workflow_id,
                    )
                )
                return pause_output(MATERIAL_CONFLICT, material_detail)

            await audit(
                AuditEvent(
                    event_id=f"{params.execution_id}:CONFLICT_RESOLVED:{rebases}",
                    task_id=params.task_id,
                    execution_id=params.execution_id,
                    event_type="CONFLICT_RESOLVED",
                    stage="BRANCH_SYNC",
                    new_state="IMPLEMENTING",
                    decision="auto_resolve",
                    reason=f"auto-resolved {len(resolution.resolved)} trivial conflicts",
                    workflow_id=workflow_id,
                )
            )
            resolved_files.update(resolution.resolved)

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
                await audit(
                    AuditEvent(
                        event_id=f"{params.execution_id}:ISSUE_RECHECK_PAUSED",
                        task_id=params.task_id,
                        execution_id=params.execution_id,
                        event_type="ISSUE_RECHECK_PAUSED",
                        stage="BRANCH_SYNC",
                        new_state="paused",
                        decision="pause",
                        reason=recheck.reason,
                        workflow_id=workflow_id,
                    )
                )
                return pause_output(recheck.category, recheck.reason)

        await audit(
            AuditEvent(
                event_id=f"{params.execution_id}:BRANCH_PROMOTED",
                task_id=params.task_id,
                execution_id=params.execution_id,
                event_type="BRANCH_PROMOTED",
                stage="BRANCH_SYNC",
                prev_state="IMPLEMENTING",
                new_state="IMPLEMENTING",
                decision="promote",
                reason=f"branch ready for PR at {base_head}",
                workflow_id=workflow_id,
            )
        )
        return BranchSyncOutput(
            execution_id=params.execution_id,
            status="synced",
            terminal_reason=clean_reason,
            base_head=base_head,
            rebases=rebases,
            drift=drift,
        )
