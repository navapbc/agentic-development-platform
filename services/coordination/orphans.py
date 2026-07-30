"""Registration of externally created artifacts for orphan tracking."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from database.models.schema import orphan_artifacts
from scripts.config import get_settings
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BRANCH = "branch"
PR = "pr"
BUILD = "build"
EVIDENCE = "evidence"
STAGING = "staging"
ACTIVE = "active"


@dataclass(frozen=True)
class OrphanArtifact:
    artifact_type: str
    external_id: str
    repository: str = ""
    owning_workflow: str = ""
    ttl_at: datetime | None = None
    platform_tagged: bool = True


def orphan_id_for(artifact_type: str, external_id: str, repository: str) -> str:
    canonical = f"{artifact_type}\x1f{external_id}\x1f{repository}"
    return hashlib.sha256(canonical.encode()).hexdigest()


class OrphanRegistry(Protocol):
    async def register(self, artifact: OrphanArtifact) -> str: ...


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


class InMemoryOrphanRegistry:
    def __init__(self) -> None:
        self.records: dict[str, OrphanArtifact] = {}

    async def register(self, artifact: OrphanArtifact) -> str:
        orphan_id = orphan_id_for(artifact.artifact_type, artifact.external_id, artifact.repository)
        self.records.setdefault(orphan_id, artifact)
        return orphan_id
