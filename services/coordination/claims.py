"""Atomic work claims and a file-impact scheduler for non-colliding parallel agents."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from database.models.schema import work_claims
from scripts.config import get_settings
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

CLAIMED = "claimed"
RELEASED = "released"
COMPLETED = "completed"

OwnerActive = Callable[[str], Awaitable[bool]]


async def _is_reclaimable(
    state: str, owner_workflow_id: str | None, owner_active: OwnerActive | None
) -> bool:
    if state == COMPLETED:
        return False
    if state == RELEASED:
        return True
    if not owner_workflow_id:
        return False
    if owner_active is None:
        return False
    try:
        return not await owner_active(owner_workflow_id)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class WorkUnit:
    task_id: str
    work_unit: str
    execution_id: str
    agent_id: str
    file_impact: tuple[str, ...] = ()
    workflow_id: str = ""


@dataclass(frozen=True)
class ClaimOutcome:
    claim_id: str
    won: bool
    claim_version: int


@dataclass(frozen=True)
class Schedule:
    admitted: tuple[WorkUnit, ...]
    deferred: tuple[WorkUnit, ...]


def claim_id_for(task_id: str, work_unit: str) -> str:
    return hashlib.sha256(f"{task_id}\x1f{work_unit}".encode()).hexdigest()


def partition_by_file_impact(units: Iterable[WorkUnit]) -> Schedule:
    claimed_files: set[str] = set()
    admitted: list[WorkUnit] = []
    deferred: list[WorkUnit] = []
    for unit in units:
        files = set(unit.file_impact)
        if files & claimed_files:
            deferred.append(unit)
        else:
            admitted.append(unit)
            claimed_files |= files
    return Schedule(tuple(admitted), tuple(deferred))


class WorkClaimStore(Protocol):
    async def try_claim(self, unit: WorkUnit) -> ClaimOutcome: ...

    async def transition(
        self, claim_id: str, *, expected_version: int, new_state: str
    ) -> int | None: ...

    async def release(self, claim_id: str, *, owner_workflow_id: str) -> bool: ...

    async def complete(self, claim_id: str, *, owner_workflow_id: str) -> bool: ...


class PostgresWorkClaimStore:
    def __init__(
        self, engine: AsyncEngine | None = None, *, owner_active: OwnerActive | None = None
    ) -> None:
        self._engine = engine
        self._owner_active = owner_active

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def try_claim(self, unit: WorkUnit) -> ClaimOutcome:
        claim_id = claim_id_for(unit.task_id, unit.work_unit)
        engine = self._get_engine()
        async with engine.begin() as conn:
            inserted = (
                await conn.execute(
                    insert(work_claims)
                    .values(
                        claim_id=claim_id,
                        task_id=unit.task_id,
                        work_unit=unit.work_unit,
                        execution_id=unit.execution_id,
                        agent_id=unit.agent_id,
                        file_impact=list(unit.file_impact) or None,
                        owner_workflow_id=unit.workflow_id or None,
                        state=CLAIMED,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[work_claims.c.task_id, work_claims.c.work_unit]
                    )
                    .returning(work_claims.c.claim_version)
                )
            ).first()
            if inserted is not None:
                return ClaimOutcome(claim_id, True, inserted.claim_version)
            existing = (
                await conn.execute(
                    select(
                        work_claims.c.claim_version,
                        work_claims.c.state,
                        work_claims.c.owner_workflow_id,
                    ).where(work_claims.c.claim_id == claim_id)
                )
            ).first()
            if existing is None:
                return ClaimOutcome(claim_id, False, 0)
            if not await _is_reclaimable(
                existing.state, existing.owner_workflow_id, self._owner_active
            ):
                return ClaimOutcome(claim_id, False, existing.claim_version)
            taken = (
                await conn.execute(
                    update(work_claims)
                    .where(
                        work_claims.c.claim_id == claim_id,
                        work_claims.c.claim_version == existing.claim_version,
                    )
                    .values(
                        execution_id=unit.execution_id,
                        agent_id=unit.agent_id,
                        file_impact=list(unit.file_impact) or None,
                        owner_workflow_id=unit.workflow_id or None,
                        state=CLAIMED,
                        released_at=None,
                        claim_version=work_claims.c.claim_version + 1,
                    )
                    .returning(work_claims.c.claim_version)
                )
            ).first()
            if taken is not None:
                return ClaimOutcome(claim_id, True, taken.claim_version)
            current = (
                await conn.execute(
                    select(work_claims.c.claim_version).where(work_claims.c.claim_id == claim_id)
                )
            ).first()
        return ClaimOutcome(claim_id, False, current.claim_version if current else 0)

    async def transition(
        self, claim_id: str, *, expected_version: int, new_state: str
    ) -> int | None:
        engine = self._get_engine()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(work_claims)
                    .where(
                        work_claims.c.claim_id == claim_id,
                        work_claims.c.claim_version == expected_version,
                    )
                    .values(state=new_state, claim_version=work_claims.c.claim_version + 1)
                    .returning(work_claims.c.claim_version)
                )
            ).first()
        return row.claim_version if row is not None else None

    async def release(self, claim_id: str, *, owner_workflow_id: str) -> bool:
        engine = self._get_engine()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(work_claims)
                    .where(
                        work_claims.c.claim_id == claim_id,
                        work_claims.c.owner_workflow_id == owner_workflow_id,
                        work_claims.c.state != COMPLETED,
                    )
                    .values(state=RELEASED, released_at=func.now())
                    .returning(work_claims.c.claim_id)
                )
            ).first()
        return row is not None

    async def complete(self, claim_id: str, *, owner_workflow_id: str) -> bool:
        engine = self._get_engine()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(work_claims)
                    .where(
                        work_claims.c.claim_id == claim_id,
                        work_claims.c.owner_workflow_id == owner_workflow_id,
                        work_claims.c.state == CLAIMED,
                    )
                    .values(state=COMPLETED, released_at=func.now())
                    .returning(work_claims.c.claim_id)
                )
            ).first()
        return row is not None


class InMemoryWorkClaimStore:
    def __init__(self, *, owner_active: OwnerActive | None = None) -> None:
        self._claims: dict[str, dict[str, object]] = {}
        self._owner_active = owner_active

    async def try_claim(self, unit: WorkUnit) -> ClaimOutcome:
        claim_id = claim_id_for(unit.task_id, unit.work_unit)
        existing = self._claims.get(claim_id)
        if existing is not None:
            reclaimable = await _is_reclaimable(
                str(existing["state"]),
                str(existing.get("owner_workflow_id") or ""),
                self._owner_active,
            )
            if not reclaimable:
                return ClaimOutcome(claim_id, False, int(existing["claim_version"]))
            existing["claim_version"] = int(existing["claim_version"]) + 1
            existing["state"] = CLAIMED
            existing["agent_id"] = unit.agent_id
            existing["execution_id"] = unit.execution_id
            existing["owner_workflow_id"] = unit.workflow_id
            return ClaimOutcome(claim_id, True, int(existing["claim_version"]))
        self._claims[claim_id] = {
            "state": CLAIMED,
            "claim_version": 1,
            "agent_id": unit.agent_id,
            "execution_id": unit.execution_id,
            "owner_workflow_id": unit.workflow_id,
        }
        return ClaimOutcome(claim_id, True, 1)

    async def transition(
        self, claim_id: str, *, expected_version: int, new_state: str
    ) -> int | None:
        row = self._claims.get(claim_id)
        if row is None or int(row["claim_version"]) != expected_version:
            return None
        row["claim_version"] = int(row["claim_version"]) + 1
        row["state"] = new_state
        return int(row["claim_version"])

    async def release(self, claim_id: str, *, owner_workflow_id: str) -> bool:
        row = self._claims.get(claim_id)
        if (
            row is None
            or str(row.get("owner_workflow_id") or "") != owner_workflow_id
            or row["state"] == COMPLETED
        ):
            return False
        row["state"] = RELEASED
        return True

    async def complete(self, claim_id: str, *, owner_workflow_id: str) -> bool:
        row = self._claims.get(claim_id)
        if row is None or str(row.get("owner_workflow_id") or "") != owner_workflow_id:
            return False
        if row["state"] != CLAIMED:
            return False
        row["state"] = COMPLETED
        return True
