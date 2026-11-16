"""Sequence-gate tests: in-order actions proceed, stale ones discard with a receipt."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from services.coordination.sequencing import (
    ADMITTED,
    DISCARDED,
    STALE_REVIEW_DISCARDED,
    InMemorySequenceGate,
    PostgresSequenceGate,
    SequencedAction,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine


def _action(sequence: int, **overrides) -> SequencedAction:
    values = {
        "task_id": "AGENTDEV-1",
        "execution_id": "e1",
        "sequence": sequence,
        "idempotency_key": f"k{sequence}",
        "operation": "jira_comment",
    }
    values.update(overrides)
    return SequencedAction(**values)


async def test_first_action_is_admitted():
    gate = InMemorySequenceGate()
    decision = await gate.admit(_action(1))
    assert decision.outcome == ADMITTED
    assert decision.current_sequence == 1
    assert decision.receipt is None


async def test_in_order_actions_advance_the_gate():
    gate = InMemorySequenceGate()
    await gate.admit(_action(1))
    decision = await gate.admit(_action(2))
    assert decision.outcome == ADMITTED
    assert decision.current_sequence == 2
    assert gate.receipts == []


async def test_out_of_order_action_is_discarded_with_receipt():
    gate = InMemorySequenceGate()
    await gate.admit(_action(1))
    await gate.admit(_action(3))

    stale = await gate.admit(_action(2))

    assert stale.outcome == DISCARDED
    assert stale.receipt == STALE_REVIEW_DISCARDED
    assert stale.current_sequence == 3
    assert len(gate.receipts) == 1
    assert gate.receipts[0].sequence == 2


async def test_replayed_sequence_is_stale():
    gate = InMemorySequenceGate()
    await gate.admit(_action(1))
    decision = await gate.admit(_action(1))
    assert decision.outcome == DISCARDED
    assert decision.receipt == STALE_REVIEW_DISCARDED


async def test_in_order_action_proceeds_after_a_stale_discard():
    gate = InMemorySequenceGate()
    await gate.admit(_action(1))
    await gate.admit(_action(2))

    stale = await gate.admit(_action(1))
    fresh = await gate.admit(_action(3))

    assert stale.outcome == DISCARDED
    assert fresh.outcome == ADMITTED
    assert fresh.current_sequence == 3


async def test_sequences_are_tracked_per_task():
    gate = InMemorySequenceGate()
    await gate.admit(_action(5, task_id="AGENTDEV-1"))
    other = await gate.admit(_action(1, task_id="AGENTDEV-2"))
    assert other.outcome == ADMITTED
    assert other.current_sequence == 1


async def test_reset_clears_the_gate_high_water():
    gate = InMemorySequenceGate()
    await gate.admit(SequencedAction(task_id="AGENTDEV-1", execution_id="exec-1", sequence=5))

    await gate.reset("AGENTDEV-1", tenant="")
    decision = await gate.admit(
        SequencedAction(task_id="AGENTDEV-1", execution_id="exec-2", sequence=1)
    )

    assert decision.outcome == ADMITTED


async def test_reentrant_action_is_admitted_at_the_sequence_it_already_holds():
    gate = InMemorySequenceGate()
    await gate.admit(_action(10))

    decision = await gate.admit(_action(10, reentrant=True))

    assert decision.outcome == ADMITTED
    assert decision.current_sequence == 10


async def test_reentrant_action_is_discarded_once_a_newer_run_advances_the_gate():
    gate = InMemorySequenceGate()
    await gate.admit(_action(10))
    await gate.admit(_action(20, execution_id="e2"))

    decision = await gate.admit(_action(10, reentrant=True))

    assert decision.outcome == DISCARDED
    assert decision.current_sequence == 20
    assert decision.receipt == STALE_REVIEW_DISCARDED


async def test_two_tenants_sequence_the_same_task_key_independently():
    gate = InMemorySequenceGate()
    await gate.admit(_action(5, tenant="alpha"))

    decision = await gate.admit(_action(1, tenant="beta"))

    assert decision.outcome == ADMITTED
    assert decision.current_sequence == 1


async def test_reset_clears_only_the_named_tenant_s_gate():
    gate = InMemorySequenceGate()
    await gate.admit(_action(5, tenant="alpha"))
    await gate.admit(_action(5, tenant="beta"))

    await gate.reset("AGENTDEV-1", tenant="alpha")

    assert (await gate.admit(_action(1, tenant="alpha"))).outcome == ADMITTED
    assert (await gate.admit(_action(1, tenant="beta"))).outcome == DISCARDED


class _StatementCapturingEngine:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, object]]] = []

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
        self._engine.statements.append((str(compiled), dict(compiled.params)))
        return SimpleNamespace(first=lambda: None)


async def test_the_postgres_gate_reads_and_advances_the_tenant_s_row():
    engine = _StatementCapturingEngine()
    gate = PostgresSequenceGate(cast(AsyncEngine, engine))

    await gate.admit(_action(3, tenant="alpha"))

    (read_sql, read_params), (write_sql, write_params) = engine.statements
    assert "sequence_gate.tenant = %(tenant_1)s" in read_sql
    assert read_params["tenant_1"] == "alpha"
    assert "ON CONFLICT (tenant, task_id)" in write_sql
    assert write_params["tenant"] == "alpha"


async def test_the_postgres_gate_reset_deletes_only_the_tenant_s_row():
    engine = _StatementCapturingEngine()
    gate = PostgresSequenceGate(cast(AsyncEngine, engine))

    await gate.reset("AGENTDEV-1", tenant="alpha")

    ((sql, params),) = engine.statements
    assert sql.startswith("DELETE FROM agent_platform.sequence_gate")
    assert params == {"tenant_1": "alpha", "task_id_1": "AGENTDEV-1"}
