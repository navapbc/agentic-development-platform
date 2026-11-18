"""Read-only access to one tenant's persisted evidence artifacts for the gateway."""

from __future__ import annotations

from typing import Any, Protocol

from database.engine import shared_platform_engine
from database.models.schema import evidence_artifacts
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

SECURITY_COMPONENT = "security-scanner"


class EvidenceReader(Protocol):
    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None: ...

    async def get_security_findings(self, execution_id: str) -> dict[str, Any] | None: ...


class PostgresEvidenceReader:
    """Read the evidence recorded for one tenant.

    An artifact recorded for another tenant, or for no tenant, reads as absent, the same as an
    ID that names no artifact.
    """

    def __init__(self, tenant: str, engine: AsyncEngine | None = None) -> None:
        if not tenant:
            raise ValueError("the evidence reader needs the tenant whose executions it reads")
        self._tenant = tenant
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = shared_platform_engine()
        return self._engine

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        stmt = (
            select(
                evidence_artifacts.c.artifact_id,
                evidence_artifacts.c.execution_id,
                evidence_artifacts.c.content_hash,
                evidence_artifacts.c.source_component,
                evidence_artifacts.c.classification,
                evidence_artifacts.c.retention,
                evidence_artifacts.c.location,
                evidence_artifacts.c.metadata,
            )
            .where(evidence_artifacts.c.artifact_id == artifact_id)
            .where(evidence_artifacts.c.tenant == self._tenant)
        )
        engine = self._get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        return dict(row) if row is not None else None

    async def get_security_findings(self, execution_id: str) -> dict[str, Any] | None:
        stmt = (
            select(evidence_artifacts.c.metadata)
            .where(evidence_artifacts.c.execution_id == execution_id)
            .where(evidence_artifacts.c.source_component == SECURITY_COMPONENT)
            .where(evidence_artifacts.c.tenant == self._tenant)
            .limit(1)
        )
        engine = self._get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None or not row.metadata:
            return None
        return dict(row.metadata)
