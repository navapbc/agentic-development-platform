"""Sequence-gate tests: in-order actions proceed, stale ones discard with a receipt."""

from __future__ import annotations

from services.coordination.sequencing import (
    ADMITTED,
    DISCARDED,
    STALE_REVIEW_DISCARDED,
    InMemorySequenceGate,
    SequencedAction,
)


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

    await gate.reset("AGENTDEV-1")
    decision = await gate.admit(
        SequencedAction(task_id="AGENTDEV-1", execution_id="exec-2", sequence=1)
    )

    assert decision.outcome == ADMITTED
