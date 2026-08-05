"""Tests for the branch-synchronization workflow stage."""

from __future__ import annotations

import uuid

from services.orchestration.branch_sync import (
    CONFLICTS,
    FAST_FORWARD,
    REBASED,
    ConflictPayload,
)
from services.orchestration.temporal_activities.branch_sync import (
    RecheckRequest,
    RecheckResult,
    ResolveRequest,
    ResolveResult,
    SyncRequest,
    SyncResult,
)
from services.orchestration.temporal_activities.persistence import AuditEvent, ExecutionState
from services.orchestration.temporal_workflows.branch_sync import (
    BranchSyncInput,
    BranchSyncWorkflow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


async def _run_workflow(
    *,
    sync_outcomes: list[SyncResult],
    resolutions: list[ResolveResult] | None = None,
    recheck: RecheckResult | None = None,
    issue_key: str = "AGENTDEV-1",
    rebase_cap: int = 2,
):
    states: list[str] = []
    audits: list[AuditEvent] = []
    sync_calls: list[SyncRequest] = []
    resolve_calls: list[ResolveRequest] = []
    recheck_calls: list[RecheckRequest] = []
    sync_queue = list(sync_outcomes)
    resolve_queue = list(resolutions or [])

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        audits.append(event)
        return event.event_id

    @activity.defn(name="synchronize_branch")
    async def fake_sync(req: SyncRequest) -> SyncResult:
        sync_calls.append(req)
        return sync_queue.pop(0)

    @activity.defn(name="resolve_conflicts")
    async def fake_resolve(req: ResolveRequest) -> ResolveResult:
        resolve_calls.append(req)
        return resolve_queue.pop(0)

    @activity.defn(name="recheck_issue")
    async def fake_recheck(req: RecheckRequest) -> RecheckResult:
        recheck_calls.append(req)
        return recheck or RecheckResult(category="UNCHANGED", requires_pause=False)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"sync-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[BranchSyncWorkflow],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules("beartype")
            ),
            activities=[fake_persist, fake_audit, fake_sync, fake_resolve, fake_recheck],
        ):
            result = await env.client.execute_workflow(
                BranchSyncWorkflow.run,
                BranchSyncInput(
                    task_id="T-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    base_sha="a" * 40,
                    branch="agent/T-1/exec-1",
                    workspace_path="/tmp/ws",
                    issue_key=issue_key,
                    stored_issue={"summary": "do the thing"},
                    rebase_cap=rebase_cap,
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, states, audits, sync_calls, resolve_calls, recheck_calls


def _event_types(audits: list[AuditEvent]) -> list[str]:
    return [a.event_type for a in audits]


def _conflict_sync(conflict: ConflictPayload) -> SyncResult:
    return SyncResult(
        status=CONFLICTS,
        base_head="b" * 40,
        human_commits=False,
        conflicts=[conflict],
    )


async def test_unchanged_base_fast_forwards_to_synced():
    result, _, audits, _, resolve_calls, recheck_calls = await _run_workflow(
        sync_outcomes=[SyncResult(status=FAST_FORWARD, base_head="a" * 40, human_commits=False)],
    )

    assert result.status == "synced"
    assert result.terminal_reason == FAST_FORWARD
    assert result.escalated is False
    assert result.rebases == 0
    assert resolve_calls == []
    assert len(recheck_calls) == 1
    assert "BRANCH_SYNCED" in _event_types(audits)
    assert "BRANCH_PROMOTED" in _event_types(audits)


async def test_advanced_base_with_trivial_conflict_resolves_and_continues():
    conflict = ConflictPayload(path="shared.py", ours="x = 1\n", theirs="x = 1\n")
    result, _, audits, sync_calls, resolve_calls, _ = await _run_workflow(
        sync_outcomes=[
            _conflict_sync(conflict),
            SyncResult(status=REBASED, base_head="b" * 40, human_commits=False),
        ],
        resolutions=[ResolveResult(status="resolved", resolved={"shared.py": "x = 1\n"})],
    )

    assert result.status == "synced"
    assert result.terminal_reason == REBASED
    assert result.rebases == 1
    assert result.drift is True
    assert len(resolve_calls) == 1
    assert sync_calls[1].resolved_files == {"shared.py": "x = 1\n"}
    types = _event_types(audits)
    assert "BRANCH_DRIFT_DETECTED" in types
    assert "CONFLICT_RESOLVED" in types
    assert "BRANCH_PROMOTED" in types


async def test_advanced_base_with_material_conflict_pauses():
    conflict = ConflictPayload(path="calc.py", ours="a\n", theirs="b\n", base="c\n")
    result, _, audits, _, _, recheck_calls = await _run_workflow(
        sync_outcomes=[
            _conflict_sync(conflict),
        ],
        resolutions=[ResolveResult(status="paused", material=["calc.py"])],
    )

    assert result.status == "paused"
    assert result.terminal_reason == "material_conflict"
    assert result.escalated is True
    assert recheck_calls == []
    types = _event_types(audits)
    assert "CONFLICT_PAUSE" in types
    assert "BRANCH_PROMOTED" not in types
    pause = next(a for a in audits if a.event_type == "CONFLICT_PAUSE")
    assert pause.new_state == "paused"
    assert pause.decision == "pause"


async def test_material_jira_edit_pauses_before_pr():
    result, _, audits, _, _, recheck_calls = await _run_workflow(
        sync_outcomes=[SyncResult(status=FAST_FORWARD, base_head="a" * 40, human_commits=False)],
        recheck=RecheckResult(
            category="MATERIAL_CHANGE",
            requires_pause=True,
            changed_fields=["summary"],
            reason="summary changed",
        ),
    )

    assert result.status == "paused"
    assert result.terminal_reason == "MATERIAL_CHANGE"
    assert result.escalated is True
    assert len(recheck_calls) == 1
    types = _event_types(audits)
    assert "ISSUE_RECHECK_PAUSED" in types
    assert "BRANCH_PROMOTED" not in types


async def test_human_commits_pauses_without_force_push():
    result, _, audits, _, resolve_calls, recheck_calls = await _run_workflow(
        sync_outcomes=[SyncResult(status=REBASED, base_head="b" * 40, human_commits=True)],
    )

    assert result.status == "paused"
    assert result.terminal_reason == "human_commits"
    assert resolve_calls == []
    assert recheck_calls == []
    assert "BRANCH_SYNC_PAUSED" in _event_types(audits)


async def test_rebase_cap_exhaustion_pauses():
    conflict = ConflictPayload(path="calc.py", ours="a\n", theirs="b\n", base="c\n")
    result, _, audits, sync_calls, resolve_calls, _ = await _run_workflow(
        sync_outcomes=[
            _conflict_sync(conflict),
            _conflict_sync(conflict),
        ],
        resolutions=[ResolveResult(status="resolved", resolved={"calc.py": "a\n"})],
        rebase_cap=1,
    )

    assert result.status == "paused"
    assert result.terminal_reason == "rebase_cap_exceeded"
    assert result.escalated is True
    assert len(sync_calls) == 2
    assert len(resolve_calls) == 1
    pause = next(a for a in audits if a.event_type == "BRANCH_SYNC_PAUSED")
    assert pause.new_state == "paused"
