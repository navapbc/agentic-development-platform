"""Reconcile executions whose workflow is gone so the concurrency cap self-heals.

A run occupies a concurrency slot while its execution row is non-terminal. When a
workflow never starts, is terminated, or times out, the row can remain non-terminal
with no live workflow behind it and hold a slot forever. This reconciler detects
those orphans and marks them terminal so admission reclaims the slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True)
class ActiveExecution:
    execution_id: str
    task_id: str
    workflow_id: str
    updated_at: datetime
    state: str = ""


class ActiveExecutionStore(Protocol):
    async def list_active_executions(self) -> list[ActiveExecution]: ...

    async def mark_orphaned(self, execution_id: str, *, reason: str) -> bool: ...


class WorkflowLiveness(Protocol):
    async def is_live(self, workflow_id: str) -> bool: ...


@dataclass(frozen=True)
class ReconcileReport:
    scanned: int
    orphaned: int


def workflow_id_for(execution: ActiveExecution) -> str:
    """Return the execution's workflow id, reconstructing the deterministic name when unset."""
    if execution.workflow_id:
        return execution.workflow_id
    return f"draft-pr-{execution.task_id}-{execution.execution_id}"


def orphan_reason(execution: ActiveExecution, workflow_id: str) -> str:
    """Describe why the execution was reclaimed, recording the state it was reclaimed from."""
    return f"workflow {workflow_id} not live (state={execution.state or 'unknown'})"


async def reconcile_orphaned_executions(
    *,
    store: ActiveExecutionStore,
    liveness: WorkflowLiveness,
    now: datetime,
    grace: timedelta,
) -> ReconcileReport:
    active = await store.list_active_executions()
    orphaned = 0
    for execution in active:
        if now - execution.updated_at < grace:
            continue
        wf_id = workflow_id_for(execution)
        if await liveness.is_live(wf_id):
            continue
        if await store.mark_orphaned(
            execution.execution_id, reason=orphan_reason(execution, wf_id)
        ):
            orphaned += 1
    return ReconcileReport(scanned=len(active), orphaned=orphaned)
