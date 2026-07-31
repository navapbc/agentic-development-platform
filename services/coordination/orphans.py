"""Registration of externally created artifacts for orphan tracking."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from database.models.schema import orphan_artifacts
from scripts.config import get_settings
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BRANCH = "branch"
PR = "pr"
BUILD = "build"
EVIDENCE = "evidence"
STAGING = "staging"
ACTIVE = "active"
REAPED = "reaped"
ARCHIVED = "archived"


@dataclass(frozen=True)
class OrphanArtifact:
    artifact_type: str
    external_id: str
    repository: str = ""
    owning_workflow: str = ""
    ttl_at: datetime | None = None
    platform_tagged: bool = True


@dataclass(frozen=True)
class OrphanRecord:
    orphan_id: str
    artifact_type: str
    external_id: str
    repository: str
    state: str
    ttl_at: datetime | None
    platform_tagged: bool


def orphan_id_for(artifact_type: str, external_id: str, repository: str) -> str:
    canonical = f"{artifact_type}\x1f{external_id}\x1f{repository}"
    return hashlib.sha256(canonical.encode()).hexdigest()


class OrphanRegistry(Protocol):
    async def register(self, artifact: OrphanArtifact) -> str: ...

    async def list_reapable(self, now: datetime) -> list[OrphanRecord]: ...

    async def mark_state(self, orphan_id: str, state: str) -> None: ...


class PostgresOrphanRegistry:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def register(self, artifact: OrphanArtifact) -> str:
        orphan_id = orphan_id_for(artifact.artifact_type, artifact.external_id, artifact.repository)
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                insert(orphan_artifacts)
                .values(
                    id=orphan_id,
                    artifact_type=artifact.artifact_type,
                    external_id=artifact.external_id,
                    repository=artifact.repository or None,
                    owning_workflow=artifact.owning_workflow or None,
                    state=ACTIVE,
                    ttl_at=artifact.ttl_at,
                    platform_tagged=artifact.platform_tagged,
                )
                .on_conflict_do_nothing(index_elements=[orphan_artifacts.c.id])
            )
        return orphan_id

    async def list_reapable(self, now: datetime) -> list[OrphanRecord]:
        engine = self._get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                select(orphan_artifacts).where(
                    orphan_artifacts.c.state == ACTIVE,
                    orphan_artifacts.c.ttl_at.is_not(None),
                    orphan_artifacts.c.ttl_at <= now,
                )
            )
            rows = result.mappings().all()
        return [
            OrphanRecord(
                orphan_id=row["id"],
                artifact_type=row["artifact_type"],
                external_id=row["external_id"],
                repository=row["repository"] or "",
                state=row["state"],
                ttl_at=row["ttl_at"],
                platform_tagged=bool(row["platform_tagged"]),
            )
            for row in rows
        ]

    async def mark_state(self, orphan_id: str, state: str) -> None:
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                update(orphan_artifacts)
                .where(orphan_artifacts.c.id == orphan_id)
                .values(state=state)
            )


class InMemoryOrphanRegistry:
    def __init__(self) -> None:
        self.records: dict[str, OrphanArtifact] = {}
        self.states: dict[str, str] = {}

    async def register(self, artifact: OrphanArtifact) -> str:
        orphan_id = orphan_id_for(artifact.artifact_type, artifact.external_id, artifact.repository)
        self.records.setdefault(orphan_id, artifact)
        self.states.setdefault(orphan_id, ACTIVE)
        return orphan_id

    async def list_reapable(self, now: datetime) -> list[OrphanRecord]:
        reapable: list[OrphanRecord] = []
        for orphan_id, artifact in self.records.items():
            if self.states.get(orphan_id, ACTIVE) != ACTIVE:
                continue
            if artifact.ttl_at is None or artifact.ttl_at > now:
                continue
            reapable.append(
                OrphanRecord(
                    orphan_id=orphan_id,
                    artifact_type=artifact.artifact_type,
                    external_id=artifact.external_id,
                    repository=artifact.repository,
                    state=self.states.get(orphan_id, ACTIVE),
                    ttl_at=artifact.ttl_at,
                    platform_tagged=artifact.platform_tagged,
                )
            )
        return reapable

    async def mark_state(self, orphan_id: str, state: str) -> None:
        self.states[orphan_id] = state
