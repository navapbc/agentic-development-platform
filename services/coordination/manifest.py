"""Determinism manifest: the first audit event of an execution, pinning its inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from database.models.schema import audit_events, executions
from scripts.config import get_settings
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

MANIFEST_EVENT_TYPE = "determinism_manifest"


@dataclass(frozen=True)
class DeterminismManifest:
    execution_id: str
    task_id: str
    snapshot_version: int
    repository_sha: str = ""
    policy_bundle_version: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    resolved_models: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "repository_sha": self.repository_sha,
            "policy_bundle_version": self.policy_bundle_version,
            "prompt_versions": dict(self.prompt_versions),
            "resolved_models": dict(self.resolved_models),
        }


@dataclass(frozen=True)
class ManifestRecord:
    event_id: str
    manifest: DeterminismManifest


def manifest_event_id(execution_id: str) -> str:
    return hashlib.sha256(f"{MANIFEST_EVENT_TYPE}:{execution_id}".encode()).hexdigest()


def _execution_columns(manifest: DeterminismManifest) -> dict[str, Any]:
    return {
        "repository_sha": manifest.repository_sha or None,
        "policy_bundle_version": manifest.policy_bundle_version or None,
        "prompt_versions": dict(manifest.prompt_versions) or None,
        "resolved_models": dict(manifest.resolved_models) or None,
    }


class ManifestStore(Protocol):
    async def record(self, manifest: DeterminismManifest) -> ManifestRecord: ...


class PostgresManifestStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def record(self, manifest: DeterminismManifest) -> ManifestRecord:
        event_id = manifest_event_id(manifest.execution_id)
        engine = self._get_engine()
        async with engine.begin() as conn:
            await conn.execute(
                update(executions)
                .where(executions.c.execution_id == manifest.execution_id)
                .values(**_execution_columns(manifest))
            )
            await conn.execute(
                insert(audit_events)
                .values(
                    event_id=event_id,
                    task_id=manifest.task_id,
                    execution_id=manifest.execution_id,
                    event_type=MANIFEST_EVENT_TYPE,
                    policy_version=manifest.policy_bundle_version or None,
                    correlation_ids=manifest.to_dict(),
                )
                .on_conflict_do_nothing(index_elements=[audit_events.c.event_id])
            )
        return ManifestRecord(event_id, manifest)


class InMemoryManifestStore:
    def __init__(self) -> None:
        self.executions: dict[str, dict[str, Any]] = {}
        self.events: list[ManifestRecord] = []

    async def record(self, manifest: DeterminismManifest) -> ManifestRecord:
        event_id = manifest_event_id(manifest.execution_id)
        self.executions[manifest.execution_id] = _execution_columns(manifest)
        record = ManifestRecord(event_id, manifest)
        if all(existing.event_id != event_id for existing in self.events):
            self.events.append(record)
        return record
