"""Immutable, content-addressed task snapshots captured from a normalized Jira issue."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import audit_events, task_snapshots
from scripts.config import Settings, get_settings
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from services.integration.jira.credentials import acceptance_field, allowed_projects
from services.integration.jira.normalize import normalize_issue


class AllowlistError(Exception):
    """Raised when an issue's project is not in the configured allowlist."""


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class TaskSnapshot:
    snapshot_id: str
    execution_id: str
    task_id: str
    jira_project: str
    jira_source: str
    version: int
    content_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SnapshotCandidate:
    execution_id: str
    task_id: str
    jira_project: str
    jira_source: str
    content_hash: str
    payload: dict[str, Any]


def snapshot_id_for(execution_id: str, version: int) -> str:
    return hashlib.sha256(f"{execution_id}:{version}".encode()).hexdigest()


def _intake_event_id(snapshot_id: str) -> str:
    return hashlib.sha256(f"task_snapshot:{snapshot_id}".encode()).hexdigest()


class TaskSnapshotStore(Protocol):
    async def persist(self, candidate: SnapshotCandidate) -> TaskSnapshot: ...


class PostgresTaskSnapshotStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def persist(self, candidate: SnapshotCandidate) -> TaskSnapshot:
        engine = self._get_engine()
        async with engine.begin() as conn:
            latest = (
                await conn.execute(
                    select(
                        task_snapshots.c.snapshot_id,
                        task_snapshots.c.version,
                        task_snapshots.c.content_hash,
                    )
                    .where(task_snapshots.c.execution_id == candidate.execution_id)
                    .order_by(task_snapshots.c.version.desc())
                    .limit(1)
                )
            ).first()
            if latest is not None and latest.content_hash == candidate.content_hash:
                return _snapshot_from(candidate, latest.snapshot_id, latest.version)
            version = latest.version + 1 if latest is not None else 1
            snapshot_id = snapshot_id_for(candidate.execution_id, version)
            await conn.execute(
                insert(task_snapshots)
                .values(
                    snapshot_id=snapshot_id,
                    execution_id=candidate.execution_id,
                    task_id=candidate.task_id,
                    jira_project=candidate.jira_project,
                    jira_source=candidate.jira_source,
                    version=version,
                    content_hash=candidate.content_hash,
                    payload=candidate.payload,
                )
                .on_conflict_do_nothing(index_elements=[task_snapshots.c.snapshot_id])
            )
            await conn.execute(
                insert(audit_events)
                .values(
                    event_id=_intake_event_id(snapshot_id),
                    task_id=candidate.task_id,
                    execution_id=candidate.execution_id,
                    event_type="task_snapshot_captured",
                    new_state=f"v{version}",
                    artifact_hashes={"content_hash": candidate.content_hash},
                )
                .on_conflict_do_nothing(index_elements=[audit_events.c.event_id])
            )
        return _snapshot_from(candidate, snapshot_id, version)


def _snapshot_from(candidate: SnapshotCandidate, snapshot_id: str, version: int) -> TaskSnapshot:
    return TaskSnapshot(
        snapshot_id=snapshot_id,
        execution_id=candidate.execution_id,
        task_id=candidate.task_id,
        jira_project=candidate.jira_project,
        jira_source=candidate.jira_source,
        version=version,
        content_hash=candidate.content_hash,
        payload=candidate.payload,
    )


def _enforce_allowlist(project: str, settings: Settings) -> None:
    allowed = allowed_projects(settings)
    if project not in allowed:
        raise AllowlistError(
            f"jira project {project!r} is not allowlisted (allowed: {sorted(allowed)})"
        )


async def capture_task_snapshot(
    raw_issue: dict[str, Any],
    *,
    execution_id: str,
    jira_source: str,
    settings: Settings | None = None,
    store: TaskSnapshotStore | None = None,
    raw_comments: tuple[Any, ...] = (),
) -> TaskSnapshot:
    settings = settings or get_settings()
    normalized = normalize_issue(
        raw_issue,
        raw_comments=raw_comments,
        acceptance_field=acceptance_field(settings),
    )
    _enforce_allowlist(normalized.project, settings)
    payload = normalized.to_dict()
    candidate = SnapshotCandidate(
        execution_id=execution_id,
        task_id=normalized.task_id,
        jira_project=normalized.project,
        jira_source=jira_source,
        content_hash=content_hash(payload),
        payload=payload,
    )
    store = store or PostgresTaskSnapshotStore()
    return await store.persist(candidate)
