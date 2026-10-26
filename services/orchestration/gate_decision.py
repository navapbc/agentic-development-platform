"""Signal an approve or reject decision to a workflow parked at a configured human gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from database.models.schema import executions
from scripts.config import Settings, get_settings, to_host_hostport, to_host_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_GATE_DECISION_SIGNAL = "submit_gate_decision"
_AWAITING_HUMAN = "AWAITING_HUMAN"


@dataclass(frozen=True)
class GateDecisionResult:
    issue_key: str
    gate: str
    approved: bool
    signalled: tuple[str, ...]

    @property
    def resumed(self) -> bool:
        return bool(self.signalled)

    @property
    def message(self) -> str:
        verb = "approve" if self.approved else "reject"
        if not self.signalled:
            return f"{self.issue_key} has no workflow parked at a human gate, nothing to resume"
        workflows = ", ".join(self.signalled)
        return f"{self.issue_key} sent a {verb} decision for gate {self.gate} to {workflows}"


class GateDecisionStore(Protocol):
    async def parked_workflow_ids(self, issue_key: str) -> list[str]: ...


class GateSignaller(Protocol):
    async def signal_gate_decision(
        self, workflow_id: str, *, gate: str, approved: bool, actor: str
    ) -> None: ...


async def resume_gate(
    issue_key: str,
    *,
    gate: str,
    approved: bool,
    actor: str,
    store: GateDecisionStore,
    signaller: GateSignaller,
) -> GateDecisionResult:
    parked = await store.parked_workflow_ids(issue_key)
    for workflow_id in parked:
        await signaller.signal_gate_decision(workflow_id, gate=gate, approved=approved, actor=actor)
    return GateDecisionResult(issue_key, gate=gate, approved=approved, signalled=tuple(parked))


class PostgresGateDecisionStore:
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


class TemporalGateSignaller:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def signal_gate_decision(
        self, workflow_id: str, *, gate: str, approved: bool, actor: str
    ) -> None:
        from temporalio.client import Client
        from temporalio.service import RPCError, RPCStatusCode

        client = await Client.connect(
            to_host_hostport(self._settings.temporal_host),
            namespace=self._settings.temporal_namespace,
        )
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(_GATE_DECISION_SIGNAL, args=[gate, approved, actor])
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return
            raise


async def run_gate_decision(
    issue_key: str,
    *,
    gate: str,
    approved: bool,
    actor: str = "operator",
    settings: Any = None,
) -> GateDecisionResult:
    resolved = settings or get_settings()
    engine = create_async_engine(
        to_host_url(resolved.database_url), future=True, pool_pre_ping=True
    )
    try:
        return await resume_gate(
            issue_key,
            gate=gate,
            approved=approved,
            actor=actor,
            store=PostgresGateDecisionStore(engine),
            signaller=TemporalGateSignaller(resolved),
        )
    finally:
        await engine.dispose()
