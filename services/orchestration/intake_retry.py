"""Reset a failed Jira issue's intake state so the next poll relaunches it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import executions, sequence_gate, side_effects
from scripts.config import Settings, get_settings, to_host_hostport, to_host_url
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_INTAKE_START = "intake_start"
_TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED")

WorkflowActive = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True)
class IntakeRetryResult:
    issue_key: str
    released: int
    superseded: int
    blocked_by: tuple[str, ...]

    @property
    def retriggered(self) -> bool:
        return not self.blocked_by and self.released > 0

    @property
    def message(self) -> str:
        if self.blocked_by:
            running = ", ".join(self.blocked_by)
            return f"{self.issue_key} still has a running workflow ({running}), not retriggered"
        if self.released == 0:
            return f"{self.issue_key} had no intake record to clear, nothing to retrigger"
        return (
            f"{self.issue_key} cleared for retrigger, released {self.released} intake record(s) "
            f"and marked {self.superseded} stale execution(s) failed"
        )


class IntakeRetryStore(Protocol):
    async def workflow_ids(self, issue_key: str) -> list[str]: ...

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int: ...

    async def mark_non_terminal_failed(self, issue_key: str) -> int: ...

    async def clear_sequence_gate(self, issue_key: str) -> int: ...


async def retry_intake(
    issue_key: str,
    *,
    store: IntakeRetryStore,
    workflow_active: WorkflowActive,
    repo: str | None = None,
) -> IntakeRetryResult:
    blocked = [wid for wid in await store.workflow_ids(issue_key) if await workflow_active(wid)]
    if blocked:
        return IntakeRetryResult(issue_key, released=0, superseded=0, blocked_by=tuple(blocked))
    released = await store.clear_intake_dedup(issue_key, repo)
    superseded = await store.mark_non_terminal_failed(issue_key) if released else 0
    if released:
        await store.clear_sequence_gate(issue_key)
    return IntakeRetryResult(issue_key, released=released, superseded=superseded, blocked_by=())


class PostgresIntakeRetryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def workflow_ids(self, issue_key: str) -> list[str]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(executions.c.workflow_id)
                    .where(executions.c.task_id == issue_key)
                    .where(executions.c.workflow_id.is_not(None))
                    .distinct()
                )
            ).all()
        return [row.workflow_id for row in rows]

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int:
        clause = (side_effects.c.task_id == issue_key) & (
            side_effects.c.operation == _INTAKE_START
        )
        if repo is not None:
            clause = clause & side_effects.c.target.like(f"%:{repo}")
        async with self._engine.begin() as conn:
            return (await conn.execute(delete(side_effects).where(clause))).rowcount or 0

    async def mark_non_terminal_failed(self, issue_key: str) -> int:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    update(executions)
                    .where(executions.c.task_id == issue_key)
                    .where(executions.c.state.not_in(_TERMINAL_STATES))
                    .values(state="FAILED")
                )
            ).rowcount or 0

    async def clear_sequence_gate(self, issue_key: str) -> int:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    delete(sequence_gate).where(sequence_gate.c.task_id == issue_key)
                )
            ).rowcount or 0


def temporal_workflow_active(settings: Settings) -> WorkflowActive:
    async def _active(workflow_id: str) -> bool:
        from temporalio.client import Client, WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        try:
            client = await Client.connect(
                to_host_hostport(settings.temporal_host), namespace=settings.temporal_namespace
            )
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            return exc.status != RPCStatusCode.NOT_FOUND
        except Exception:  # noqa: BLE001
            return True
        return description.status == WorkflowExecutionStatus.RUNNING

    return _active


async def run_intake_retry(
    issue_key: str, *, repo: str | None = None, settings: Any = None
) -> IntakeRetryResult:
    resolved = settings or get_settings()
    engine = create_async_engine(
        to_host_url(resolved.database_url), future=True, pool_pre_ping=True
    )
    try:
        return await retry_intake(
            issue_key,
            store=PostgresIntakeRetryStore(engine),
            workflow_active=temporal_workflow_active(resolved),
            repo=repo,
        )
    finally:
        await engine.dispose()
