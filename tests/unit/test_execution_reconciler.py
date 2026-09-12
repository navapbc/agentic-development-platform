"""Unit tests for the orphaned-execution reconciler that self-heals the concurrency cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.orchestration.execution_reconciler import (
    ActiveExecution,
    reconcile_orphaned_executions,
    workflow_id_for,
)

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_GRACE = timedelta(seconds=120)


class FakeStore:
    def __init__(self, active: list[ActiveExecution], *, unmarkable: set[str] | None = None):
        self._active = active
        self._unmarkable = unmarkable or set()
        self.marked: list[tuple[str, str]] = []

    async def list_active_executions(self) -> list[ActiveExecution]:
        return list(self._active)

    async def mark_orphaned(self, execution_id: str, *, reason: str) -> bool:
        self.marked.append((execution_id, reason))
        return execution_id not in self._unmarkable


class FakeLiveness:
    def __init__(self, live_ids: set[str]):
        self._live = live_ids
        self.checked: list[str] = []

    async def is_live(self, workflow_id: str) -> bool:
        self.checked.append(workflow_id)
        return workflow_id in self._live


def _exec(
    execution_id: str,
    *,
    task_id: str = "T-1",
    workflow_id: str = "",
    age_seconds: int = 600,
):
    return ActiveExecution(
        execution_id=execution_id,
        task_id=task_id,
        workflow_id=workflow_id,
        updated_at=_NOW - timedelta(seconds=age_seconds),
    )


def test_workflow_id_reconstructed_when_unset():
    execution = _exec("intake-abc", task_id="4606-b5a9", workflow_id="")
    assert workflow_id_for(execution) == "draft-pr-4606-b5a9-intake-abc"


def test_workflow_id_prefers_stored_value():
    execution = _exec("intake-abc", workflow_id="draft-pr-custom")
    assert workflow_id_for(execution) == "draft-pr-custom"


@pytest.mark.asyncio
async def test_orphan_with_no_live_workflow_is_reclaimed():
    store = FakeStore([_exec("intake-dead", workflow_id="draft-pr-dead")])
    liveness = FakeLiveness(live_ids=set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, now=_NOW, grace=_GRACE
    )

    assert report == type(report)(scanned=1, orphaned=1)
    assert store.marked == [("intake-dead", "workflow draft-pr-dead not live")]


@pytest.mark.asyncio
async def test_live_workflow_is_left_alone():
    store = FakeStore([_exec("intake-live", workflow_id="draft-pr-live")])
    liveness = FakeLiveness(live_ids={"draft-pr-live"})

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, now=_NOW, grace=_GRACE
    )

    assert report.orphaned == 0
    assert store.marked == []


@pytest.mark.asyncio
async def test_recent_execution_is_skipped_within_grace():
    store = FakeStore([_exec("intake-new", workflow_id="draft-pr-new", age_seconds=10)])
    liveness = FakeLiveness(live_ids=set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, now=_NOW, grace=_GRACE
    )

    assert report.orphaned == 0
    assert liveness.checked == []
    assert store.marked == []


@pytest.mark.asyncio
async def test_unset_workflow_id_is_reconstructed_for_liveness_check():
    store = FakeStore([_exec("intake-x", task_id="9fb6", workflow_id="")])
    liveness = FakeLiveness(live_ids=set())

    await reconcile_orphaned_executions(store=store, liveness=liveness, now=_NOW, grace=_GRACE)

    assert liveness.checked == ["draft-pr-9fb6-intake-x"]


@pytest.mark.asyncio
async def test_lost_race_to_terminal_is_not_counted():
    store = FakeStore(
        [_exec("intake-raced", workflow_id="draft-pr-raced")],
        unmarkable={"intake-raced"},
    )
    liveness = FakeLiveness(live_ids=set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, now=_NOW, grace=_GRACE
    )

    assert report.scanned == 1
    assert report.orphaned == 0
