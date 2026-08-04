"""Registry of parked workflows so a reconciler can find and resume them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from database.models.schema import suspended_flows
from scripts.config import get_settings
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@dataclass(frozen=True)
class SuspensionRecord:
    workflow_id: str
    reason: str
    resume_on: str
    parked_build_id: str = ""
    stage: str = ""
    detail: str = ""


class SuspensionStore(Protocol):
    async def record(self, record: SuspensionRecord) -> None: ...

    async def clear(self, workflow_id: str) -> None: ...

    async def list_open(self) -> list[SuspensionRecord]: ...


class InMemorySuspensionStore:
    def __init__(self) -> None:
        self._rows: dict[str, SuspensionRecord] = {}

    async def record(self, record: SuspensionRecord) -> None:
        self._rows[record.workflow_id] = record

    async def clear(self, workflow_id: str) -> None:
        self._rows.pop(workflow_id, None)

    async def list_open(self) -> list[SuspensionRecord]:
        return list(self._rows.values())


class PostgresSuspensionStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def record(self, record: SuspensionRecord) -> None:
        stmt = (
            insert(suspended_flows)
            .values(
                workflow_id=record.workflow_id,
                reason=record.reason,
                resume_on=record.resume_on,
                parked_build_id=record.parked_build_id,
                stage=record.stage,
                detail=record.detail or None,
            )
            .on_conflict_do_update(
                index_elements=[suspended_flows.c.workflow_id],
                set_={
                    "reason": record.reason,
                    "resume_on": record.resume_on,
                    "parked_build_id": record.parked_build_id,
                    "stage": record.stage,
                    "detail": record.detail or None,
                },
            )
        )
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(stmt)

    async def clear(self, workflow_id: str) -> None:
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                delete(suspended_flows).where(suspended_flows.c.workflow_id == workflow_id)
            )

    async def list_open(self) -> list[SuspensionRecord]:
        engine = self._get_engine()
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        suspended_flows.c.workflow_id,
                        suspended_flows.c.reason,
                        suspended_flows.c.resume_on,
                        suspended_flows.c.parked_build_id,
                        suspended_flows.c.stage,
                        suspended_flows.c.detail,
                    )
                )
            ).all()
        return [
            SuspensionRecord(
                workflow_id=row.workflow_id,
                reason=row.reason,
                resume_on=row.resume_on,
                parked_build_id=row.parked_build_id or "",
                stage=row.stage or "",
                detail=row.detail or "",
            )
            for row in rows
        ]
