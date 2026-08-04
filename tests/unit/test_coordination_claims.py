"""Atomic-claim and file-impact scheduler tests: one winner, non-overlapping units."""

from __future__ import annotations

import asyncio

from services.coordination.claims import (
    CLAIMED,
    COMPLETED,
    InMemoryWorkClaimStore,
    WorkUnit,
    claim_id_for,
    partition_by_file_impact,
)


def _unit(work_unit: str, *files: str, task_id: str = "AGENTDEV-1") -> WorkUnit:
    return WorkUnit(
        task_id=task_id,
        work_unit=work_unit,
        execution_id="exec-1",
        agent_id=f"agent-{work_unit}",
        file_impact=files,
    )


def test_claim_id_is_deterministic_per_task_and_unit():
    assert claim_id_for("AGENTDEV-1", "u1") == claim_id_for("AGENTDEV-1", "u1")
    assert claim_id_for("AGENTDEV-1", "u1") != claim_id_for("AGENTDEV-1", "u2")


async def test_first_claim_wins():
    store = InMemoryWorkClaimStore()
    outcome = await store.try_claim(_unit("u1"))
    assert outcome.won is True
    assert outcome.claim_version == 1


async def test_concurrent_claims_resolve_to_exactly_one_winner():
    store = InMemoryWorkClaimStore()
    unit = _unit("u1")
    outcomes = await asyncio.gather(store.try_claim(unit), store.try_claim(unit))
    assert [o.won for o in outcomes].count(True) == 1
    assert {o.claim_id for o in outcomes} == {claim_id_for("AGENTDEV-1", "u1")}


async def test_distinct_units_both_win():
    store = InMemoryWorkClaimStore()
    first = await store.try_claim(_unit("u1"))
    second = await store.try_claim(_unit("u2"))
    assert first.won and second.won


async def test_transition_uses_optimistic_concurrency():
    store = InMemoryWorkClaimStore()
    outcome = await store.try_claim(_unit("u1"))
    bumped = await store.transition(outcome.claim_id, expected_version=1, new_state=COMPLETED)
    assert bumped == 2
    stale = await store.transition(outcome.claim_id, expected_version=1, new_state=CLAIMED)
    assert stale is None


def test_scheduler_defers_units_that_share_files():
    schedule = partition_by_file_impact(
        [
            _unit("u1", "a.py", "b.py"),
            _unit("u2", "b.py", "c.py"),
            _unit("u3", "d.py"),
        ]
    )
    admitted = {u.work_unit for u in schedule.admitted}
    deferred = {u.work_unit for u in schedule.deferred}
    assert admitted == {"u1", "u3"}
    assert deferred == {"u2"}


def _owned(work_unit: str, workflow_id: str, *, agent: str = "agent-a") -> WorkUnit:
    return WorkUnit(
        task_id="AGENTDEV-1",
        work_unit=work_unit,
        execution_id=f"exec-{agent}",
        agent_id=agent,
        workflow_id=workflow_id,
    )


async def test_active_owner_blocks_reclaim():
    async def owner_active(_workflow_id: str) -> bool:
        return True

    store = InMemoryWorkClaimStore(owner_active=owner_active)
    first = await store.try_claim(_owned("u1", "wf-live", agent="agent-a"))
    second = await store.try_claim(_owned("u1", "wf-new", agent="agent-b"))
    assert first.won is True
    assert second.won is False
    assert second.claim_version == 1


async def test_dead_owner_is_reclaimed():
    async def owner_active(_workflow_id: str) -> bool:
        return False

    store = InMemoryWorkClaimStore(owner_active=owner_active)
    first = await store.try_claim(_owned("u1", "wf-crashed", agent="agent-a"))
    second = await store.try_claim(_owned("u1", "wf-new", agent="agent-b"))
    assert first.won is True
    assert second.won is True
    assert second.claim_version == 2


async def test_empty_owner_preserves_single_winner():
    async def owner_active(_workflow_id: str) -> bool:
        return False

    store = InMemoryWorkClaimStore(owner_active=owner_active)
    first = await store.try_claim(_unit("u1"))
    second = await store.try_claim(_unit("u1"))
    assert first.won is True
    assert second.won is False


async def test_released_claim_is_reclaimable():
    store = InMemoryWorkClaimStore()
    first = await store.try_claim(_owned("u1", "wf-a", agent="agent-a"))
    await store.transition(first.claim_id, expected_version=1, new_state="released")
    second = await store.try_claim(_owned("u1", "wf-b", agent="agent-b"))
    assert second.won is True


async def test_completed_claim_is_not_reclaimable():
    async def owner_active(_workflow_id: str) -> bool:
        return False

    store = InMemoryWorkClaimStore(owner_active=owner_active)
    first = await store.try_claim(_owned("u1", "wf-a", agent="agent-a"))
    await store.transition(first.claim_id, expected_version=1, new_state=COMPLETED)
    second = await store.try_claim(_owned("u1", "wf-b", agent="agent-b"))
    assert second.won is False


def test_scheduler_admits_all_disjoint_units():
    schedule = partition_by_file_impact(
        [_unit("u1", "a.py"), _unit("u2", "b.py"), _unit("u3", "c.py")]
    )
    assert len(schedule.admitted) == 3
    assert schedule.deferred == ()
