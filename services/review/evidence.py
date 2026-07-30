"""Evidence-artifact persistence for verified review attestations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from database.models.schema import evidence_artifacts
from scripts.config import get_settings
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@dataclass(frozen=True)
class EvidenceRecord:
    artifact_id: str
    execution_id: str
    content_hash: str
    source_component: str
    classification: str
    retention: str | None = None
    location: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EvidenceSink(Protocol):
    async def persist(self, record: EvidenceRecord) -> str: ...


class PostgresEvidenceSink:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def persist(self, record: EvidenceRecord) -> str:
        stmt = (
            insert(evidence_artifacts)
            .values(
                artifact_id=record.artifact_id,
                execution_id=record.execution_id,
                content_hash=record.content_hash,
                source_component=record.source_component,
                classification=record.classification,
                retention=record.retention,
                location=record.location,
                metadata=record.metadata or None,
            )
            .on_conflict_do_nothing(index_elements=[evidence_artifacts.c.content_hash])
        )
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(stmt)
        return record.artifact_id
