from __future__ import annotations

import pytest
from services.orchestration.intake_cursor import CursorRelease
from services.orchestration.intake_retry import WorkflowLiveness
from services.orchestration.task_restart import RestartResult, restart_task


class _RepairStore:
    def __init__(self, parked: list[str]) -> None:
        self._parked = parked

    async def parked_workflow_ids(self, issue_key: str) -> list[str]:
        return self._parked


class _RepairSignaller:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool, str]] = []

    async def signal_repair_decision(self, workflow_id: str, *, retry: bool, actor: str) -> None:
        self.sent.append((workflow_id, retry, actor))


class _IntakeStore:
    def __init__(self, *, workflow_ids: list[str], dedup: int) -> None:
        self._workflow_ids = workflow_ids
        self._dedup = dedup
        self.cleared_repo: str | None = "unset"
        self.marked = False
        self.gate_cleared = False
        self.claims_cleared = False

    async def workflow_ids(self, issue_key: str) -> list[str]:
        return self._workflow_ids

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int:
        self.cleared_repo = repo
        return self._dedup

    async def mark_non_terminal_failed(self, issue_key: str) -> int:
        self.marked = True
        return 1

    async def clear_sequence_gate(self, issue_key: str) -> int:
        self.gate_cleared = True
        return 1

    async def clear_work_claims(self, issue_key: str) -> int:
        self.claims_cleared = True
        return 1


def _active(active: set[str]):
    async def _check(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.RUNNING if workflow_id in active else WorkflowLiveness.ENDED

    return _check


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_restart_abandons_then_retriggers_intake():
    wf = "draft-pr-ADSER-24-intake-39d2c862"
    repair_signaller = _RepairSignaller()
    intake_store = _IntakeStore(workflow_ids=[wf], dedup=1)

    result = await restart_task(
        "ADSER-24",
        repair_store=_RepairStore([wf]),
        repair_signaller=repair_signaller,
        intake_store=intake_store,
        liveness=_active(set()),
        sleep=_no_sleep,
    )

    assert isinstance(result, RestartResult)
    assert result.abandoned == (wf,)
    assert repair_signaller.sent == [(wf, False, "operator")]
    assert intake_store.marked is True
    assert intake_store.gate_cleared is True
    assert result.restarted
    assert "restarted" in result.message


@pytest.mark.asyncio
async def test_restart_clears_the_work_claim_and_says_so():
    wf = "draft-pr-ADSER-24-intake-39d2c862"
    intake_store = _IntakeStore(workflow_ids=[wf], dedup=1)

    result = await restart_task(
        "ADSER-24",
        repair_store=_RepairStore([wf]),
        repair_signaller=_RepairSignaller(),
        intake_store=intake_store,
        liveness=_active(set()),
        sleep=_no_sleep,
    )

    assert intake_store.claims_cleared is True
    assert result.retry.claims_cleared == 1
    assert "dropped 1 work claim(s)" in result.message
    assert "dropped 1 intake dedup record(s)" in result.message


@pytest.mark.asyncio
async def test_restart_waits_for_the_abandoned_workflow_to_settle():
    wf = "draft-pr-ADSER-24-intake-39d2c862"
    settling = {wf}
    checks = {"n": 0}

    async def _active_then_idle(workflow_id: str) -> WorkflowLiveness:
        checks["n"] += 1
        if checks["n"] >= 3:
            settling.discard(workflow_id)
        return WorkflowLiveness.RUNNING if workflow_id in settling else WorkflowLiveness.ENDED

    result = await restart_task(
        "ADSER-24",
        repair_store=_RepairStore([wf]),
        repair_signaller=_RepairSignaller(),
        intake_store=_IntakeStore(workflow_ids=[wf], dedup=1),
        liveness=_active_then_idle,
        settle_delay=0.0,
        sleep=_no_sleep,
    )

    assert result.restarted
    assert checks["n"] >= 3


@pytest.mark.asyncio
async def test_restart_with_no_parked_workflow_still_retriggers():
    intake_store = _IntakeStore(workflow_ids=[], dedup=1)
    repair_signaller = _RepairSignaller()

    result = await restart_task(
        "ADSER-24",
        repair_store=_RepairStore([]),
        repair_signaller=repair_signaller,
        intake_store=intake_store,
        liveness=_active(set()),
        sleep=_no_sleep,
    )

    assert result.abandoned == ()
    assert repair_signaller.sent == []
    assert result.restarted
    assert "no parked workflow to abandon" in result.message


@pytest.mark.asyncio
async def test_restart_is_blocked_when_a_workflow_is_still_running():
    wf = "draft-pr-ADSER-24-intake-live"
    result = await restart_task(
        "ADSER-24",
        repair_store=_RepairStore([]),
        repair_signaller=_RepairSignaller(),
        intake_store=_IntakeStore(workflow_ids=[wf], dedup=1),
        liveness=_active({wf}),
        sleep=_no_sleep,
    )

    assert not result.restarted
    assert result.retry.blocked_by == (wf,)
    assert "not restarted" in result.message


@pytest.mark.asyncio
async def test_restart_reports_an_unreachable_probe_without_claiming_a_workflow_runs():
    wf = "draft-pr-ADSER-25"
    intake_store = _IntakeStore(workflow_ids=[wf], dedup=1)

    async def _unreachable(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.UNKNOWN

    result = await restart_task(
        "ADSER-25",
        repair_store=_RepairStore([]),
        repair_signaller=_RepairSignaller(),
        intake_store=intake_store,
        liveness=_unreachable,
        settle_delay=0.0,
        sleep=_no_sleep,
    )

    assert not result.restarted
    assert result.retry.undetermined == (wf,)
    assert intake_store.marked is False
    assert "still has a running workflow" not in result.message
    assert "could not determine" in result.message


class _Cursor:
    def __init__(self, held: int = 1, warning: str = "") -> None:
        self._held = held
        self._warning = warning
        self.released: list[str] = []

    async def release(self, ticket_key: str) -> CursorRelease:
        self.released.append(ticket_key)
        return CursorRelease(self._held, self._warning)


@pytest.mark.asyncio
async def test_restart_releases_the_poll_cursor_for_the_ticket_it_abandoned():
    wf = "draft-pr-ADEV-40-intake-39d2c862"
    cursor = _Cursor()

    result = await restart_task(
        "ADEV-40",
        repair_store=_RepairStore([wf]),
        repair_signaller=_RepairSignaller(),
        intake_store=_IntakeStore(workflow_ids=[wf], dedup=0),
        liveness=_active(set()),
        settle_delay=0.0,
        sleep=_no_sleep,
        cursor=cursor,
    )

    assert cursor.released == ["ADEV-40"]
    assert result.restarted


@pytest.mark.asyncio
async def test_a_cursor_warning_does_not_withhold_the_abandon_or_the_restart():
    wf = "draft-pr-ADEV-41-intake-39d2c862"
    repair_signaller = _RepairSignaller()
    cursor = _Cursor(warning="the rebar store does not list ADEV-41 as ready")

    result = await restart_task(
        "ADEV-41",
        repair_store=_RepairStore([wf]),
        repair_signaller=repair_signaller,
        intake_store=_IntakeStore(workflow_ids=[wf], dedup=1),
        liveness=_active(set()),
        settle_delay=0.0,
        sleep=_no_sleep,
        cursor=cursor,
    )

    assert result.restarted
    assert result.abandoned == (wf,)
    assert repair_signaller.sent == [(wf, False, "operator")]
    assert "does not list ADEV-41 as ready" in result.message
