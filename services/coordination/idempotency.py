"""Deterministic side-effect idempotency: persist-before-effect, check-then-act."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import side_effects
from scripts.config import get_settings
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_COMPLETED = "completed"
_PENDING = "pending"
_INTAKE_START = "intake_start"
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
    reentrant: bool = False


def side_effect_key(ref: SideEffectRef) -> str:
    canonical = _UNIT_SEPARATOR.join((ref.task_id, ref.execution_id, ref.operation, ref.target))
    return hashlib.sha256(canonical.encode()).hexdigest()


def intake_start_target_repo(operation: str, target: str) -> str:
    if operation != _INTAKE_START:
        return ""
    _, separator, repo = target.rpartition(":")
    if not separator:
        return ""
    return repo


def _intake_start_repo(ref: SideEffectRef) -> str:
    return intake_start_target_repo(ref.operation, ref.target)


def _same_intake_start_scope(left: SideEffectRef, right: SideEffectRef) -> bool:
    left_repo = _intake_start_repo(left)
    return (
        bool(left_repo)
        and left.task_id == right.task_id
        and left.execution_id == right.execution_id
        and left.operation == right.operation
        and left_repo == _intake_start_repo(right)
    )


def _matching_ref_key(refs: Mapping[str, SideEffectRef], ref: SideEffectRef) -> str | None:
    key = side_effect_key(ref)
    if key in refs:
        return key
    for candidate_key, candidate in refs.items():
        if _same_intake_start_scope(ref, candidate):
            return candidate_key
    return None


async def _resolve(effect: Effect) -> Any:
    result = effect()
    if inspect.isawaitable(result):
        return await result
    return result


class SideEffectLedger(Protocol):
    async def reserve(self, ref: SideEffectRef) -> SideEffectOutcome: ...

    async def record(self, ref: SideEffectRef, result: Any) -> SideEffectOutcome: ...

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

    async def reserve(self, ref: SideEffectRef) -> SideEffectOutcome:
        key = side_effect_key(ref)
        engine = self._get_engine()
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(
                        side_effects.c.side_effect_key,
                        side_effects.c.status,
                        side_effects.c.result,
                    ).where(side_effects.c.side_effect_key == key)
                )
            ).first()
            repo = _intake_start_repo(ref)
            if existing is None and repo:
                legacy_rows = (
                    await conn.execute(
                        select(
                            side_effects.c.side_effect_key,
                            side_effects.c.status,
                            side_effects.c.result,
                            side_effects.c.target,
                        )
                        .where(side_effects.c.task_id == ref.task_id)
                        .where(side_effects.c.execution_id == ref.execution_id)
                        .where(side_effects.c.operation == ref.operation)
                        .order_by(side_effects.c.updated_at.desc())
                    )
                ).all()
                existing = next(
                    (
                        row
                        for row in legacy_rows
                        if intake_start_target_repo(ref.operation, str(row.target)) == repo
                    ),
                    None,
                )
            if existing is not None and existing.status == _COMPLETED:
                return SideEffectOutcome(
                    existing.side_effect_key, replayed=True, result=existing.result
                )
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
        return SideEffectOutcome(key, replayed=False, result=None, reentrant=existing is not None)

    async def record(self, ref: SideEffectRef, result: Any) -> SideEffectOutcome:
        key = side_effect_key(ref)
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                update(side_effects)
                .where(side_effects.c.side_effect_key == key)
                .values(status=_COMPLETED, result=result)
            )
        return SideEffectOutcome(key, replayed=False, result=result)

    async def run_once(self, ref: SideEffectRef, effect: Effect) -> SideEffectOutcome:
        reserved = await self.reserve(ref)
        if reserved.replayed:
            return reserved
        result = await _resolve(effect)
        return await self.record(ref, result)


class InMemorySideEffectLedger:
    def __init__(self) -> None:
        self._records: dict[str, tuple[SideEffectRef, Any]] = {}
        self._pending: dict[str, SideEffectRef] = {}

    async def reserve(self, ref: SideEffectRef) -> SideEffectOutcome:
        key = side_effect_key(ref)
        existing_key = _matching_ref_key(
            {record_key: record[0] for record_key, record in self._records.items()}, ref
        )
        if existing_key is not None:
            return SideEffectOutcome(
                existing_key, replayed=True, result=self._records[existing_key][1]
            )
        pending_key = _matching_ref_key(self._pending, ref)
        reentrant = pending_key is not None
        self._pending[key] = ref
        return SideEffectOutcome(key, replayed=False, result=None, reentrant=reentrant)

    async def record(self, ref: SideEffectRef, result: Any) -> SideEffectOutcome:
        key = side_effect_key(ref)
        self._records[key] = (ref, result)
        self._pending.pop(key, None)
        return SideEffectOutcome(key, replayed=False, result=result)

    async def run_once(self, ref: SideEffectRef, effect: Effect) -> SideEffectOutcome:
        reserved = await self.reserve(ref)
        if reserved.replayed:
            return reserved
        result = await _resolve(effect)
        return await self.record(ref, result)
