"""Reset a failed Jira issue's intake state so the next poll relaunches it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from database.models.schema import executions, sequence_gate, side_effects, work_claims
from scripts.config import Settings, get_settings, to_host_hostport, to_host_url
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from services.coordination.idempotency import intake_start_target_repo

_INTAKE_START = "intake_start"
_TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED")


class WorkflowLiveness(StrEnum):
    """Result of a liveness probe, which can also fail to reach Temporal at all.

    UNKNOWN keeps a probe failure distinguishable from a confirmed running workflow so
    callers refuse the retry without reporting the workflow as live.
    """

    RUNNING = "running"
    ENDED = "ended"
    UNKNOWN = "unknown"


WorkflowLivenessProbe = Callable[[str], Awaitable[WorkflowLiveness]]


@dataclass(frozen=True)
class IntakeRetryResult:
    issue_key: str
    intake_cleared: int
    superseded: int
    blocked_by: tuple[str, ...]
    claims_cleared: int = 0
    undetermined: tuple[str, ...] = ()

    @property
    def retriggered(self) -> bool:
        return not self.blocked_by and not self.undetermined and self.intake_cleared > 0

    @property
    def message(self) -> str:
        if self.blocked_by:
            running = ", ".join(self.blocked_by)
            return f"{self.issue_key} still has a running workflow ({running}), not retriggered"
        if self.undetermined:
            unreachable = ", ".join(self.undetermined)
            return (
                f"{self.issue_key} could not determine whether {unreachable} is still "
                "running, not retriggered"
            )
        if self.intake_cleared == 0:
            if self.claims_cleared:
                return (
                    f"{self.issue_key} had no intake record to clear, nothing to retrigger, "
                    f"dropped {self.claims_cleared} stranded work claim(s)"
                )
            return f"{self.issue_key} had no intake record to clear, nothing to retrigger"
        return (
            f"{self.issue_key} cleared for retrigger, dropped {self.intake_cleared} intake "
            f"dedup record(s), dropped {self.claims_cleared} work claim(s), and marked "
            f"{self.superseded} stale execution(s) failed"
        )


class IntakeRetryStore(Protocol):
    async def workflow_ids(self, issue_key: str) -> list[str]: ...

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int: ...

    async def mark_non_terminal_failed(self, issue_key: str) -> int: ...

    async def clear_sequence_gate(self, issue_key: str) -> int: ...

    async def clear_work_claims(self, issue_key: str) -> int: ...


async def retry_intake(
    issue_key: str,
    *,
    store: IntakeRetryStore,
    liveness: WorkflowLivenessProbe,
    repo: str | None = None,
) -> IntakeRetryResult:
    running: list[str] = []
    undetermined: list[str] = []
    for workflow_id in await store.workflow_ids(issue_key):
        state = await liveness(workflow_id)
        if state is WorkflowLiveness.RUNNING:
            running.append(workflow_id)
        elif state is WorkflowLiveness.UNKNOWN:
            undetermined.append(workflow_id)
    if running or undetermined:
        return IntakeRetryResult(
            issue_key,
            intake_cleared=0,
            superseded=0,
            blocked_by=tuple(running),
            undetermined=tuple(undetermined),
        )
    intake_cleared = await store.clear_intake_dedup(issue_key, repo)
    superseded = 0
    claims_cleared = await store.clear_work_claims(issue_key)
    if intake_cleared:
        superseded = await store.mark_non_terminal_failed(issue_key)
        await store.clear_sequence_gate(issue_key)
    return IntakeRetryResult(
        issue_key,
        intake_cleared=intake_cleared,
        superseded=superseded,
        blocked_by=(),
        claims_cleared=claims_cleared,
    )


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
        clause = (side_effects.c.task_id == issue_key) & (side_effects.c.operation == _INTAKE_START)
        async with self._engine.begin() as conn:
            if repo is None:
                return (await conn.execute(delete(side_effects).where(clause))).rowcount or 0
            candidates = (
                await conn.execute(
                    select(side_effects.c.side_effect_key, side_effects.c.target).where(clause)
                )
            ).all()
            keys = [
                row.side_effect_key
                for row in candidates
                if intake_start_target_repo(_INTAKE_START, str(row.target)) == repo
            ]
            if not keys:
                return 0
            return (
                await conn.execute(
                    delete(side_effects).where(side_effects.c.side_effect_key.in_(keys))
                )
            ).rowcount or 0

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

    async def clear_work_claims(self, issue_key: str) -> int:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    delete(work_claims).where(work_claims.c.task_id == issue_key)
                )
            ).rowcount or 0


def temporal_workflow_liveness(settings: Settings) -> WorkflowLivenessProbe:
    async def _probe(workflow_id: str) -> WorkflowLiveness:
        from temporalio.client import Client, WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        try:
            client = await Client.connect(
                to_host_hostport(settings.temporal_host), namespace=settings.temporal_namespace
            )
            description = await client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return WorkflowLiveness.ENDED
            return WorkflowLiveness.UNKNOWN
        except Exception:  # noqa: BLE001
            return WorkflowLiveness.UNKNOWN
        if description.status == WorkflowExecutionStatus.RUNNING:
            return WorkflowLiveness.RUNNING
        return WorkflowLiveness.ENDED

    return _probe


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
            liveness=temporal_workflow_liveness(resolved),
            repo=repo,
        )
    finally:
        await engine.dispose()
