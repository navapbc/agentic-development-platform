"""Signal a retry or abandon decision to a workflow parked at AWAITING_HUMAN."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import executions
from scripts.config import Settings, get_settings, to_host_hostport, to_host_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_REPAIR_DECISION_SIGNAL = "submit_repair_decision"
_AWAITING_HUMAN = "AWAITING_HUMAN"


@dataclass(frozen=True)
class RepairDecisionResult:
    issue_key: str
    retry: bool
    signalled: tuple[str, ...]

    @property
    def resumed(self) -> bool:
        return bool(self.signalled)

    @property
    def message(self) -> str:
        verb = "retry" if self.retry else "abandon"
        if not self.signalled:
            return f"{self.issue_key} has no workflow parked for human repair, nothing to resume"
        workflows = ", ".join(self.signalled)
        return f"{self.issue_key} sent a {verb} decision to {workflows}"


class RepairDecisionStore(Protocol):
    async def parked_workflow_ids(self, issue_key: str) -> list[str]: ...


class RepairSignaller(Protocol):
    async def signal_repair_decision(
        self, workflow_id: str, *, retry: bool, actor: str
    ) -> None: ...


async def resume_repair(
    issue_key: str,
    *,
    retry: bool,
    actor: str,
    store: RepairDecisionStore,
    signaller: RepairSignaller,
) -> RepairDecisionResult:
    parked = await store.parked_workflow_ids(issue_key)
    for workflow_id in parked:
        await signaller.signal_repair_decision(workflow_id, retry=retry, actor=actor)
    return RepairDecisionResult(issue_key, retry=retry, signalled=tuple(parked))


class PostgresRepairDecisionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def parked_workflow_ids(self, issue_key: str) -> list[str]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(executions.c.workflow_id)
                    .where(executions.c.task_id == issue_key)
                    .where(executions.c.state == _AWAITING_HUMAN)
                    .where(executions.c.workflow_id.is_not(None))
                    .distinct()
                )
            ).all()
        return [row.workflow_id for row in rows]


class TemporalRepairSignaller:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def signal_repair_decision(
        self, workflow_id: str, *, retry: bool, actor: str
    ) -> None:
        from temporalio.client import Client
        from temporalio.service import RPCError, RPCStatusCode

        client = await Client.connect(
            to_host_hostport(self._settings.temporal_host),
            namespace=self._settings.temporal_namespace,
        )
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(_REPAIR_DECISION_SIGNAL, args=[retry, actor])
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return
            raise


async def run_repair_decision(
    issue_key: str, *, retry: bool, actor: str = "operator", settings: Any = None
) -> RepairDecisionResult:
    resolved = settings or get_settings()
    engine = create_async_engine(
        to_host_url(resolved.database_url), future=True, pool_pre_ping=True
    )
    try:
        return await resume_repair(
            issue_key,
            retry=retry,
            actor=actor,
            store=PostgresRepairDecisionStore(engine),
            signaller=TemporalRepairSignaller(resolved),
        )
    finally:
        await engine.dispose()
