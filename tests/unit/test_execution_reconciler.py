"""Unit tests for the orphaned-execution reconciler that self-heals the concurrency cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.orchestration.execution_reconciler import (
    ORPHAN_RECLAIMED,
    ActiveExecution,
    orphan_reason,
    reconcile_orphaned_executions,
    workflow_id_for,
)
from services.orchestration.intake_runtime import OrphanReclaimAlerter
from tests.support import hermetic_settings

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


class FakeNotifier:
    def __init__(self, *, fails: bool = False):
        self._fails = fails
        self.calls: list[tuple[str, str, str]] = []

    async def reclaimed(self, execution: ActiveExecution, *, workflow_id: str, reason: str) -> None:
        self.calls.append((execution.execution_id, workflow_id, reason))
        if self._fails:
            raise RuntimeError("slack unreachable")


def _exec(
    execution_id: str,
    *,
    task_id: str = "T-1",
    workflow_id: str = "",
    age_seconds: int = 600,
    state: str = "IMPLEMENTING",
):
    return ActiveExecution(
        execution_id=execution_id,
        task_id=task_id,
        workflow_id=workflow_id,
        updated_at=_NOW - timedelta(seconds=age_seconds),
        state=state,
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
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert report == type(report)(scanned=1, orphaned=1)
    assert store.marked == [("intake-dead", "workflow draft-pr-dead not live (state=IMPLEMENTING)")]


@pytest.mark.asyncio
async def test_live_workflow_is_left_alone():
    store = FakeStore([_exec("intake-live", workflow_id="draft-pr-live")])
    liveness = FakeLiveness(live_ids={"draft-pr-live"})

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert report.orphaned == 0
    assert store.marked == []


@pytest.mark.asyncio
async def test_recent_execution_is_skipped_within_grace():
    store = FakeStore([_exec("intake-new", workflow_id="draft-pr-new", age_seconds=10)])
    liveness = FakeLiveness(live_ids=set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert report.orphaned == 0
    assert liveness.checked == []
    assert store.marked == []


@pytest.mark.asyncio
async def test_unset_workflow_id_is_reconstructed_for_liveness_check():
    store = FakeStore([_exec("intake-x", task_id="9fb6", workflow_id="")])
    liveness = FakeLiveness(live_ids=set())

    await reconcile_orphaned_executions(
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert liveness.checked == ["draft-pr-9fb6-intake-x"]


@pytest.mark.asyncio
async def test_lost_race_to_terminal_is_not_counted():
    store = FakeStore(
        [_exec("intake-raced", workflow_id="draft-pr-raced")],
        unmarkable={"intake-raced"},
    )
    liveness = FakeLiveness(live_ids=set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert report.scanned == 1
    assert report.orphaned == 0


def test_orphan_reason_carries_state():
    execution = _exec("intake-abc", state="PR_CREATED")
    assert orphan_reason(execution, "wf-1") == "workflow wf-1 not live (state=PR_CREATED)"


def test_orphan_reason_marks_unknown_state():
    execution = _exec("intake-abc", state="")
    assert orphan_reason(execution, "wf-1") == "workflow wf-1 not live (state=unknown)"


@pytest.mark.asyncio
async def test_reconciler_reclaims_dead_working_state():
    store = FakeStore([_exec("intake-dead", workflow_id="wf-dead", state="IMPLEMENTING")])
    liveness = FakeLiveness(set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert store.marked == [("intake-dead", "workflow wf-dead not live (state=IMPLEMENTING)")]
    assert report.orphaned == 1


@pytest.mark.asyncio
async def test_reconciler_still_honours_grace_for_any_state():
    store = FakeStore([_exec("intake-fresh", state="PR_CREATED", age_seconds=10)])
    liveness = FakeLiveness(set())

    report = await reconcile_orphaned_executions(
        store=store, liveness=liveness, notifier=FakeNotifier(), now=_NOW, grace=_GRACE
    )

    assert store.marked == []
    assert liveness.checked == []
    assert report.orphaned == 0


@pytest.mark.asyncio
async def test_only_a_reclaimed_execution_alerts_the_operators():
    store = FakeStore(
        [
            _exec("intake-dead", workflow_id="wf-dead"),
            _exec("intake-live", workflow_id="wf-live"),
            _exec("intake-raced", workflow_id="wf-raced"),
        ],
        unmarkable={"intake-raced"},
    )
    notifier = FakeNotifier()

    await reconcile_orphaned_executions(
        store=store,
        liveness=FakeLiveness({"wf-live"}),
        notifier=notifier,
        now=_NOW,
        grace=_GRACE,
    )

    assert notifier.calls == [
        ("intake-dead", "wf-dead", "workflow wf-dead not live (state=IMPLEMENTING)")
    ]


@pytest.mark.asyncio
async def test_a_failed_alert_does_not_stop_the_reclaim_pass():
    store = FakeStore(
        [_exec("intake-a", workflow_id="wf-a"), _exec("intake-b", workflow_id="wf-b")]
    )
    notifier = FakeNotifier(fails=True)

    report = await reconcile_orphaned_executions(
        store=store, liveness=FakeLiveness(set()), notifier=notifier, now=_NOW, grace=_GRACE
    )

    assert report.orphaned == 2
    assert [execution_id for execution_id, _, _ in notifier.calls] == ["intake-a", "intake-b"]


@pytest.mark.asyncio
async def test_a_reclaim_alert_names_the_ticket_and_audits_the_channels_it_reached(monkeypatch):
    alerts = []
    audits = []

    def _send(alert, settings):
        alerts.append(alert)
        return ["jira", "slack"]

    async def _audit(event):
        audits.append(event)

    monkeypatch.setattr("services.orchestration.notifications.send_human_intervention_alert", _send)
    execution = ActiveExecution(
        execution_id="intake-dead",
        task_id="ADSER-24",
        workflow_id="draft-pr-ADSER-24-intake-dead",
        updated_at=_NOW,
        state="PLAN_REVIEW",
        repository="agentic-demo-service",
    )
    alerter = OrphanReclaimAlerter(hermetic_settings(tenant="acme"), audit=_audit)

    await alerter.reclaimed(execution, workflow_id=execution.workflow_id, reason="not live")

    [alert] = alerts
    assert (alert.issue_key, alert.repo, alert.reason, alert.stage) == (
        "ADSER-24",
        "agentic-demo-service",
        ORPHAN_RECLAIMED,
        "PLAN_REVIEW",
    )
    assert "inv restart --issue ADSER-24 --tenant acme" in alert.remedy
    [event] = audits
    assert (event.event_type, event.prev_state, event.new_state) == (
        "EXECUTION_ORPHAN_RECLAIMED",
        "PLAN_REVIEW",
        "CANCELLED",
    )
    assert event.alert_channels == ["jira", "slack"]
    assert event.remedy == alert.remedy
