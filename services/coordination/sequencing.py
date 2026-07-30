"""Monotonic per-task sequence gate: discard stale or out-of-order side effects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import audit_events, sequence_gate
from scripts.config import get_settings
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

ADMITTED = "admitted"
DISCARDED = "discarded"
STALE_REVIEW_DISCARDED = "STALE_REVIEW_DISCARDED"


@dataclass(frozen=True)
class SequencedAction:
    task_id: str
    execution_id: str
    sequence: int
    idempotency_key: str = ""
    operation: str = ""


@dataclass(frozen=True)
class SequenceDecision:
    outcome: str
    task_id: str
    sequence: int
    current_sequence: int
    receipt: str | None


class SequenceGate(Protocol):
    async def admit(self, action: SequencedAction) -> SequenceDecision: ...


def _receipt_event_id(action: SequencedAction) -> str:
    key = (
        f"stale_discard:{action.task_id}:{action.execution_id}:"
        f"{action.sequence}:{action.idempotency_key}"
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _receipt_values(action: SequencedAction, current: int) -> dict[str, Any]:
    return {
        "event_id": _receipt_event_id(action),
        "task_id": action.task_id,
        "execution_id": action.execution_id,
        "event_type": "side_effect_discarded",
        "decision": STALE_REVIEW_DISCARDED,
        "reason": f"sequence {action.sequence} not ahead of current {current}",
        "correlation_ids": {
            "sequence": action.sequence,
            "current_sequence": current,
            "idempotency_key": action.idempotency_key,
            "operation": action.operation,
        },
    }


class PostgresSequenceGate:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def admit(self, action: SequencedAction) -> SequenceDecision:
        engine = self._get_engine()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(sequence_gate.c.current_sequence)
                    .where(sequence_gate.c.task_id == action.task_id)
                    .with_for_update()
                )
            ).first()
            current = row.current_sequence if row is not None else 0
            if action.sequence > current:
                await conn.execute(
                    insert(sequence_gate)
                    .values(task_id=action.task_id, current_sequence=action.sequence)
                    .on_conflict_do_update(
                        index_elements=[sequence_gate.c.task_id],
                        set_={"current_sequence": action.sequence},
                    )
                )
                return SequenceDecision(
                    ADMITTED, action.task_id, action.sequence, action.sequence, None
                )
            await conn.execute(
                insert(audit_events)
                .values(**_receipt_values(action, current))
                .on_conflict_do_nothing(index_elements=[audit_events.c.event_id])
            )
        return SequenceDecision(
            DISCARDED, action.task_id, action.sequence, current, STALE_REVIEW_DISCARDED
        )


class InMemorySequenceGate:
    def __init__(self) -> None:
        self._current: dict[str, int] = {}
        self.receipts: list[SequenceDecision] = []

    async def admit(self, action: SequencedAction) -> SequenceDecision:
        current = self._current.get(action.task_id, 0)
        if action.sequence > current:
            self._current[action.task_id] = action.sequence
            return SequenceDecision(
                ADMITTED, action.task_id, action.sequence, action.sequence, None
            )
        decision = SequenceDecision(
            DISCARDED, action.task_id, action.sequence, current, STALE_REVIEW_DISCARDED
        )
        self.receipts.append(decision)
        return decision
