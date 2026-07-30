"""Deterministic side-effect idempotency: persist-before-effect, check-then-act."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import side_effects
from scripts.config import get_settings
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_COMPLETED = "completed"
_PENDING = "pending"
_UNIT_SEPARATOR = "\x1f"

Effect = Callable[[], Any] | Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class SideEffectRef:
    task_id: str
    execution_id: str
    operation: str
    target: str


@dataclass(frozen=True)
class SideEffectOutcome:
    key: str
    replayed: bool
    result: Any


def side_effect_key(ref: SideEffectRef) -> str:
    canonical = _UNIT_SEPARATOR.join((ref.task_id, ref.execution_id, ref.operation, ref.target))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _resolve(effect: Effect) -> Any:
    result = effect()
    if inspect.isawaitable(result):
        return await result
    return result


class SideEffectLedger(Protocol):
    async def run_once(self, ref: SideEffectRef, effect: Effect) -> SideEffectOutcome: ...


class PostgresSideEffectLedger:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def run_once(self, ref: SideEffectRef, effect: Effect) -> SideEffectOutcome:
        key = side_effect_key(ref)
        engine = self._get_engine()
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(side_effects.c.status, side_effects.c.result).where(
                        side_effects.c.side_effect_key == key
                    )
                )
            ).first()
            if existing is not None and existing.status == _COMPLETED:
                return SideEffectOutcome(key, replayed=True, result=existing.result)
            await conn.execute(
                insert(side_effects)
                .values(
                    side_effect_key=key,
                    task_id=ref.task_id,
                    execution_id=ref.execution_id,
                    operation=ref.operation,
                    target=ref.target,
                    status=_PENDING,
                )
                .on_conflict_do_nothing(index_elements=[side_effects.c.side_effect_key])
            )
        result = await _resolve(effect)
        async with engine.begin() as conn:
            await conn.execute(
                update(side_effects)
                .where(side_effects.c.side_effect_key == key)
                .values(status=_COMPLETED, result=result)
            )
        return SideEffectOutcome(key, replayed=False, result=result)


class InMemorySideEffectLedger:
    def __init__(self) -> None:
        self._records: dict[str, Any] = {}

    async def run_once(self, ref: SideEffectRef, effect: Effect) -> SideEffectOutcome:
        key = side_effect_key(ref)
        if key in self._records:
            return SideEffectOutcome(key, replayed=True, result=self._records[key])
        result = await _resolve(effect)
        self._records[key] = result
        return SideEffectOutcome(key, replayed=False, result=result)
