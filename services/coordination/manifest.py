"""Determinism manifest: the first audit event of an execution, pinning its inputs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from database.engine import shared_platform_engine
from database.models.schema import audit_events, executions
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

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
    gate_versions: dict[str, str] = field(default_factory=dict)
    gate_config_hash: str = ""
    tenant: str = ""
    config_bundle: dict[str, Any] = field(default_factory=dict)
    runtime_profile: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "config_bundle": dict(self.config_bundle),
            "runtime_profile": self.runtime_profile,
            "snapshot_version": self.snapshot_version,
            "repository_sha": self.repository_sha,
            "policy_bundle_version": self.policy_bundle_version,
            "prompt_versions": dict(self.prompt_versions),
            "resolved_models": dict(self.resolved_models),
            "gate_versions": dict(self.gate_versions),
            "gate_config_hash": self.gate_config_hash,
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
            self._engine = shared_platform_engine()
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


async def recorded_config_bundle(
    execution_id: str, engine: AsyncEngine | None = None
) -> tuple[str, dict[str, Any]] | None:
    """Return the tenant and config bundle identity a run's manifest recorded, or None."""
    resolved = engine or shared_platform_engine()
    async with resolved.connect() as conn:
        row = (
            await conn.execute(
                select(audit_events.c.correlation_ids).where(
                    audit_events.c.event_id == manifest_event_id(execution_id)
                )
            )
        ).first()
    if row is None or not row[0] or not row[0].get("config_bundle"):
        return None
    return str(row[0].get("tenant") or ""), dict(row[0]["config_bundle"])


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
