"""Temporal activities that persist execution state and audit events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from database.models.schema import audit_events, executions
from scripts.config import get_settings
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from temporalio import activity

_engine: AsyncEngine | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, future=True, pool_pre_ping=True)
    return _engine


@dataclass
class ExecutionState:
    execution_id: str
    task_id: str
    state: str
    workflow_id: str | None = None
    repository: str | None = None
    repository_sha: str | None = None
    policy_bundle_version: str | None = None
    resolved_models: dict[str, Any] = field(default_factory=dict)


@activity.defn
async def persist_execution_state(payload: ExecutionState) -> str:
    stmt = insert(executions).values(
        execution_id=payload.execution_id,
        task_id=payload.task_id,
        workflow_id=payload.workflow_id,
        state=payload.state,
        repository=payload.repository,
        repository_sha=payload.repository_sha,
        policy_bundle_version=payload.policy_bundle_version,
        resolved_models=payload.resolved_models or None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[executions.c.execution_id],
        set_={
            "state": payload.state,
            "workflow_id": payload.workflow_id,
            "resolved_models": payload.resolved_models or None,
        },
    )
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return payload.execution_id


@dataclass
class AuditEvent:
    event_id: str
    task_id: str
    execution_id: str
    event_type: str
    stage: str | None = None
    prev_state: str | None = None
    new_state: str | None = None
    model_id: str | None = None
    reason: str | None = None


@activity.defn
async def record_audit_event(event: AuditEvent) -> str:
    stmt = (
        insert(audit_events)
        .values(
            event_id=event.event_id,
            task_id=event.task_id,
            execution_id=event.execution_id,
            event_type=event.event_type,
            stage=event.stage,
            prev_state=event.prev_state,
            new_state=event.new_state,
            model_id=event.model_id,
            reason=event.reason,
        )
        .on_conflict_do_nothing(index_elements=[audit_events.c.event_id])
    )
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return event.event_id
