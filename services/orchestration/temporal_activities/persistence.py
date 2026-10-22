"""Temporal activities that persist execution state and audit events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from database.models.schema import audit_events, executions, usage_metrics
from observability import record_stage_cost, record_stage_latency
from scripts.config import get_settings
from sqlalchemy import func
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


def execution_state_upsert(payload: ExecutionState) -> Any:
    """Build the execution upsert, refreshing updated_at on conflict.

    The column's onupdate default fires only for UPDATE statements, not for the
    set_ mapping of ON CONFLICT DO UPDATE, so updated_at has to be named here or
    it stays frozen at insert time and the orphan reconciler reads every long run
    as stale.
    """
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
    return stmt.on_conflict_do_update(
        index_elements=[executions.c.execution_id],
        set_={
            "state": payload.state,
            "workflow_id": payload.workflow_id,
            "resolved_models": payload.resolved_models or None,
            "updated_at": func.now(),
        },
    )


@activity.defn
async def persist_execution_state(payload: ExecutionState) -> str:
    stmt = execution_state_upsert(payload)
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
    workflow_id: str | None = None
    decision: str | None = None
    prompt_version: str | None = None
    artifact_hashes: dict[str, Any] = field(default_factory=dict)


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
            workflow_id=event.workflow_id,
            decision=event.decision,
            prompt_version=event.prompt_version,
            artifact_hashes=event.artifact_hashes or None,
        )
        .on_conflict_do_nothing(index_elements=[audit_events.c.event_id])
    )
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.execute(stmt)
    from services.review.evidence import persist_audit_object

    await persist_audit_object(_audit_event_body(event))
    return event.event_id


def _audit_event_body(event: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "execution_id": event.execution_id,
        "event_type": event.event_type,
        "stage": event.stage,
        "prev_state": event.prev_state,
        "new_state": event.new_state,
        "model_id": event.model_id,
        "reason": event.reason,
        "workflow_id": event.workflow_id,
        "decision": event.decision,
        "prompt_version": event.prompt_version,
        "artifact_hashes": event.artifact_hashes or {},
    }


@dataclass
class UsageMetrics:
    execution_id: str
    task_id: str
    stage: str
    workflow_id: str | None = None
    model_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


@activity.defn
async def record_usage_metrics(payload: UsageMetrics) -> str:
    stmt = insert(usage_metrics).values(
        execution_id=payload.execution_id,
        stage=payload.stage,
        task_id=payload.task_id,
        workflow_id=payload.workflow_id,
        model_id=payload.model_id,
        prompt_tokens=payload.prompt_tokens,
        completion_tokens=payload.completion_tokens,
        cache_read_tokens=payload.cache_read_tokens,
        cache_write_tokens=payload.cache_write_tokens,
        total_tokens=payload.total_tokens,
        llm_calls=payload.llm_calls,
        tool_calls=payload.tool_calls,
        cost_usd=payload.cost_usd,
        elapsed_seconds=payload.elapsed_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[usage_metrics.c.execution_id, usage_metrics.c.stage],
        set_={
            "task_id": payload.task_id,
            "workflow_id": payload.workflow_id,
            "model_id": payload.model_id,
            "prompt_tokens": payload.prompt_tokens,
            "completion_tokens": payload.completion_tokens,
            "cache_read_tokens": payload.cache_read_tokens,
            "cache_write_tokens": payload.cache_write_tokens,
            "total_tokens": payload.total_tokens,
            "llm_calls": payload.llm_calls,
            "tool_calls": payload.tool_calls,
            "cost_usd": payload.cost_usd,
            "elapsed_seconds": payload.elapsed_seconds,
        },
    )
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.execute(stmt)
    record_stage_cost(payload.stage, payload.model_id, payload.cost_usd)
    record_stage_latency(payload.stage, payload.elapsed_seconds)
    return payload.execution_id
