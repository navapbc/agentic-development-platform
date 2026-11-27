"""Atomic-claim and file-impact scheduler tests: one winner, non-overlapping units."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

from services.coordination.claims import (
    CLAIMED,
    COMPLETED,
    RELEASED,
    InMemoryWorkClaimStore,
    PostgresWorkClaimStore,
    WorkUnit,
    claim_id_for,
    partition_by_file_impact,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine


def _unit(work_unit: str, *files: str, task_id: str = "AGENTDEV-1") -> WorkUnit:
    return WorkUnit(
        task_id=task_id,
        work_unit=work_unit,
        execution_id="exec-1",
        agent_id=f"agent-{work_unit}",
        file_impact=files,
    )


def test_claim_id_is_deterministic_per_tenant_task_and_unit():
    assert claim_id_for("AGENTDEV-1", "u1", "") == claim_id_for("AGENTDEV-1", "u1", "")
    assert claim_id_for("AGENTDEV-1", "u1", "") != claim_id_for("AGENTDEV-1", "u2", "")
    assert claim_id_for("AGENTDEV-1", "u1", "alpha") != claim_id_for("AGENTDEV-1", "u1", "beta")


def test_claim_id_matches_the_digest_the_claim_tenant_migration_writes():
    untenanted = hashlib.sha256(b"AGENTDEV-1\x1fu1").hexdigest()
    tenanted = hashlib.sha256(b"AGENTDEV-1\x1fu1\x1falpha").hexdigest()

    assert claim_id_for("AGENTDEV-1", "u1", "") == untenanted
    assert claim_id_for("AGENTDEV-1", "u1", "alpha") == tenanted


async def test_two_tenants_claim_the_same_task_work_unit():
    store = InMemoryWorkClaimStore()

    alpha = await store.try_claim(replace(_unit("u1"), tenant="alpha"))
    beta = await store.try_claim(replace(_unit("u1"), tenant="beta"))

    assert alpha.won and beta.won
    assert alpha.claim_id != beta.claim_id


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
    assert {o.claim_id for o in outcomes} == {claim_id_for("AGENTDEV-1", "u1", "")}


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


def _owned_unit(work_unit: str, workflow_id: str, *, task_id: str = "AGENTDEV-1") -> WorkUnit:
    return WorkUnit(
        task_id=task_id,
        work_unit=work_unit,
        execution_id="exec-1",
        agent_id=f"agent-{work_unit}",
        workflow_id=workflow_id,
    )


async def test_release_makes_a_claim_reclaimable_and_advances_the_version():
    store = InMemoryWorkClaimStore()
    first = await store.try_claim(_owned_unit("u1", "wf-1"))

    released = await store.release(first.claim_id, owner_workflow_id="wf-1")
    reclaim = await store.try_claim(_owned_unit("u1", "wf-2"))

    assert released is True
    assert reclaim.won is True
    assert reclaim.claim_version == first.claim_version + 1


async def test_release_requires_the_matching_owner():
    store = InMemoryWorkClaimStore()
    first = await store.try_claim(_owned_unit("u1", "wf-1"))

    assert await store.release(first.claim_id, owner_workflow_id="wf-other") is False


async def test_release_refuses_a_completed_claim():
    store = InMemoryWorkClaimStore()
    first = await store.try_claim(_owned_unit("u1", "wf-1"))
    await store.transition(first.claim_id, expected_version=1, new_state=COMPLETED)

    assert await store.release(first.claim_id, owner_workflow_id="wf-1") is False


async def test_complete_retires_a_claim_for_good():
    async def owner_active(_workflow_id: str) -> bool:
        return False

    store = InMemoryWorkClaimStore(owner_active=owner_active)
    first = await store.try_claim(_owned_unit("u1", "wf-1"))

    completed = await store.complete(first.claim_id, owner_workflow_id="wf-1")
    reclaim = await store.try_claim(_owned_unit("u1", "wf-2"))

    assert completed is True
    assert reclaim.won is False


async def test_complete_requires_the_matching_owner():
    store = InMemoryWorkClaimStore()
    first = await store.try_claim(_owned_unit("u1", "wf-1"))

    assert await store.complete(first.claim_id, owner_workflow_id="wf-other") is False


class _StatementCapturingEngine:
    def __init__(self, matched: bool) -> None:
        self._matched = matched
        self.sql = ""
        self.params: dict[str, object] = {}

    def begin(self):
        return _StatementCapturingConnection(self)


class _StatementCapturingConnection:
    def __init__(self, engine: _StatementCapturingEngine) -> None:
        self._engine = engine

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        self._engine.sql = str(compiled)
        self._engine.params = dict(compiled.params)
        return SimpleNamespace(first=lambda: ("claim-1",) if self._engine._matched else None)


def _store(matched: bool) -> tuple[PostgresWorkClaimStore, _StatementCapturingEngine]:
    engine = _StatementCapturingEngine(matched)
    return PostgresWorkClaimStore(cast(AsyncEngine, engine)), engine


async def test_releasing_a_claim_stamps_released_at_for_the_owner_only():
    store, engine = _store(matched=True)

    assert await store.release("claim-1", owner_workflow_id="wf-parent") is True
    assert "SET state=" in engine.sql and "released_at=now()" in engine.sql
    assert engine.params["claim_id_1"] == "claim-1"
    assert engine.params["owner_workflow_id_1"] == "wf-parent"
    assert engine.params["state"] == RELEASED
    assert engine.params["state_1"] == COMPLETED


async def test_releasing_a_claim_another_workflow_owns_changes_nothing():
    store, _engine = _store(matched=False)

    assert await store.release("claim-1", owner_workflow_id="wf-rival") is False


async def test_completing_a_claim_requires_it_to_still_be_claimed():
    store, engine = _store(matched=True)

    assert await store.complete("claim-1", owner_workflow_id="wf-parent") is True
    assert "released_at=now()" in engine.sql
    assert engine.params["state"] == COMPLETED
    assert engine.params["state_1"] == CLAIMED
    assert engine.params["owner_workflow_id_1"] == "wf-parent"


async def test_completing_a_claim_that_is_no_longer_claimed_changes_nothing():
    store, _engine = _store(matched=False)

    assert await store.complete("claim-1", owner_workflow_id="wf-parent") is False


class _InsertCapturingConnection(_StatementCapturingConnection):
    async def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        self._engine.sql = str(compiled)
        self._engine.params = dict(compiled.params)
        return SimpleNamespace(first=lambda: SimpleNamespace(claim_version=1))


class _InsertCapturingEngine(_StatementCapturingEngine):
    def begin(self):
        return _InsertCapturingConnection(self)


async def test_a_postgres_claim_is_unique_per_tenant_task_and_work_unit():
    engine = _InsertCapturingEngine(matched=True)
    store = PostgresWorkClaimStore(cast(AsyncEngine, engine))

    outcome = await store.try_claim(replace(_unit("u1"), tenant="alpha"))

    assert outcome.won is True
    assert outcome.claim_id == claim_id_for("AGENTDEV-1", "u1", "alpha")
    assert "ON CONFLICT (tenant, task_id, work_unit) DO NOTHING" in engine.sql
    assert engine.params["tenant"] == "alpha"
    assert engine.params["claim_id"] == outcome.claim_id


async def _covering(store, path: str, *, tenant: str = "") -> list[str]:
    holders = await store.active_claims_covering(
        path, task_id="AGENTDEV-1", work_unit="u1", tenant=tenant
    )
    return [holder.task_id for holder in holders]


async def test_an_active_claim_of_another_unit_holds_the_paths_its_file_impact_covers():
    store = InMemoryWorkClaimStore()
    await store.try_claim(_unit("u1", "src", "tsconfig.json", task_id="AGENTDEV-2"))
    await store.try_claim(replace(_unit("u1", "README.md", task_id="AGENTDEV-3"), tenant="beta"))

    assert await _covering(store, "src/store.ts") == ["AGENTDEV-2"]
    assert await _covering(store, "tsconfig.json") == ["AGENTDEV-2"]
    assert await _covering(store, "srcx/store.ts") == []
    assert await _covering(store, "README.md") == []
    assert await _covering(store, "README.md", tenant="beta") == ["AGENTDEV-3"]


async def test_the_claimants_own_claim_does_not_hold_its_files():
    store = InMemoryWorkClaimStore()
    await store.try_claim(_unit("u1", "src"))

    assert await _covering(store, "src/store.ts") == []


async def test_a_released_or_completed_claim_does_not_hold_its_files():
    store = InMemoryWorkClaimStore()
    released = replace(_unit("u1", "src", task_id="AGENTDEV-2"), workflow_id="wf-2")
    completed = replace(_unit("u1", "src", task_id="AGENTDEV-3"), workflow_id="wf-3")
    released_claim = await store.try_claim(released)
    completed_claim = await store.try_claim(completed)

    await store.release(released_claim.claim_id, owner_workflow_id="wf-2")
    await store.complete(completed_claim.claim_id, owner_workflow_id="wf-3")

    assert await _covering(store, "src/store.ts") == []


async def test_a_claim_whose_owner_workflow_stopped_does_not_hold_its_files():
    async def owner_active(workflow_id: str) -> bool:
        return workflow_id == "wf-live"

    store = InMemoryWorkClaimStore(owner_active=owner_active)
    await store.try_claim(replace(_unit("u1", "src", task_id="AGENTDEV-2"), workflow_id="wf-gone"))
    await store.try_claim(replace(_unit("u1", "src", task_id="AGENTDEV-3"), workflow_id="wf-live"))

    assert await _covering(store, "src/store.ts") == ["AGENTDEV-3"]


class _RowsConnection(_StatementCapturingConnection):
    async def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        self._engine.sql = str(compiled)
        self._engine.params = dict(compiled.params)
        return SimpleNamespace(all=lambda: self._engine.rows)


class _RowsEngine(_StatementCapturingEngine):
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        super().__init__(matched=True)
        self.rows = rows

    def begin(self):
        return _RowsConnection(self)


def _claim_row(task_id: str, *files: str) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        work_unit="u1",
        execution_id="exec-2",
        agent_id="agent-u1",
        file_impact=list(files) or None,
        owner_workflow_id="wf-2",
    )


async def test_a_postgres_claim_query_reads_active_claims_and_keeps_those_covering_the_path():
    engine = _RowsEngine([_claim_row("AGENTDEV-2", "src"), _claim_row("AGENTDEV-3", "docs")])
    store = PostgresWorkClaimStore(cast(AsyncEngine, engine))

    holders = await store.active_claims_covering(
        "src/store.ts", task_id="AGENTDEV-1", work_unit="u1", tenant="alpha"
    )

    assert [(holder.task_id, holder.file_impact) for holder in holders] == [
        ("AGENTDEV-2", ("src",))
    ]
    assert CLAIMED in engine.params.values()
    assert "alpha" in engine.params.values()
