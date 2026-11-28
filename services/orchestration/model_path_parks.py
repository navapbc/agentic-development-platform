"""Find and resume the runs parked because a model call found the model path unavailable.

A parked run waits on the ``resume_model_path`` update naming its park key. The resumer reads
the key from the run's ``suspension_state`` query and sends it back, so the update is refused when
the run has already left that park, and a resume sent twice, or sent after the run parked again,
resumes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from services.orchestration.suspension import (
    RESUME_MODEL_PATH_UPDATE,
    SUSPENSION_STATE_QUERY,
    SuspendReason,
)

log = logging.getLogger("model-path-parks")

INTAKE_ACTOR = "intake"
MODEL_PATH_PROBE_ACTOR = "model_path_probe"
OPERATOR_ACTOR = "operator"
_AWAITING_HUMAN = "AWAITING_HUMAN"
_RPC_TIMEOUT = timedelta(seconds=10)
PARK_RESUME_CONCURRENCY = 4


def parked_runs_query(tenant: str, *, issue_key: str = "", workflow_id: str = "") -> Any:
    """Select the workflow IDs of the tenant's runs parked on the model path.

    The execution row carries the tenant, so the suspension row is read through it. A run whose
    workflow ended without clearing its suspension row has left ``AWAITING_HUMAN``, so it is not
    selected.
    """
    from database.models.schema import executions, suspended_flows
    from sqlalchemy import select

    from services.orchestration.execution_tenant import tenant_executions

    stmt = (
        select(suspended_flows.c.workflow_id)
        .join(executions, executions.c.workflow_id == suspended_flows.c.workflow_id)
        .where(suspended_flows.c.reason == SuspendReason.MODEL_UNAVAILABLE.value)
        .where(executions.c.state == _AWAITING_HUMAN)
        .where(tenant_executions(tenant))
    )
    if issue_key:
        stmt = stmt.where(executions.c.task_id == issue_key)
    if workflow_id:
        stmt = stmt.where(suspended_flows.c.workflow_id == workflow_id)
    return stmt.distinct().order_by(suspended_flows.c.workflow_id)


class ParkedRunFinder:
    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def _get_engine(self) -> Any:
        if self._engine is None:
            from database.engine import shared_platform_engine

            self._engine = shared_platform_engine()
        return self._engine

    async def _workflow_ids(self, stmt: Any) -> list[str]:
        async with self._get_engine().connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [row.workflow_id for row in rows]


class PostgresParkedRuns(ParkedRunFinder):
    async def find(self, tenant: str, *, issue_key: str = "", workflow_id: str = "") -> list[str]:
        return await self._workflow_ids(
            parked_runs_query(tenant, issue_key=issue_key, workflow_id=workflow_id)
        )


async def resume_park(
    client: Any,
    workflow_id: str,
    *,
    reason: SuspendReason,
    update: str,
    actor: str,
    actor_type: str,
    detail: str | None = None,
) -> bool:
    """Resume the run's park on ``reason``, returning whether this call resumed it.

    A ``detail`` other than None also requires the park's detail to equal it. The run first moves
    to the current build when that build replays its history, as `repin_if_replayable` describes.
    """
    from scripts.config import get_settings
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.service import RPCError, RPCStatusCode

    from services.orchestration.worker_versioning import repin_if_replayable

    try:
        await repin_if_replayable(client, get_settings(), workflow_id)
    except Exception:
        log.exception("could not move %s to the current build", workflow_id)
    handle = client.get_workflow_handle(workflow_id)
    try:
        state = await handle.query(SUSPENSION_STATE_QUERY, rpc_timeout=_RPC_TIMEOUT)
        if not isinstance(state, dict):
            return False
        if state.get("reason") != reason.value:
            return False
        park_key = str(state.get("park_key") or "")
        if not park_key or (detail is not None and state.get("detail") != detail):
            return False
        outcome = await handle.execute_update(
            update, args=[park_key, actor, actor_type], rpc_timeout=_RPC_TIMEOUT
        )
    except WorkflowUpdateFailedError:
        return False
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return isinstance(outcome, dict) and bool(outcome.get("applied"))


async def resume_each(
    workflow_ids: Sequence[str], resume: Callable[[str], Awaitable[bool]]
) -> list[str]:
    """Resume each run, at most PARK_RESUME_CONCURRENCY at once, and return the IDs resumed.

    Every resume finishes before the first failure is raised.
    """
    bound = asyncio.Semaphore(PARK_RESUME_CONCURRENCY)

    async def _one(workflow_id: str) -> bool:
        async with bound:
            return await resume(workflow_id)

    results = await asyncio.gather(*(_one(w) for w in workflow_ids), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [w for w, resumed in zip(workflow_ids, results, strict=True) if resumed]


class TemporalParkResumer:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def resume(self, workflow_id: str, *, actor: str, actor_type: str) -> bool:
        """Resume the run's model path park, returning whether this call resumed it."""
        return await resume_park(
            self._client,
            workflow_id,
            reason=SuspendReason.MODEL_UNAVAILABLE,
            update=RESUME_MODEL_PATH_UPDATE,
            actor=actor,
            actor_type=actor_type,
        )


class ModelPathParkRegistry:
    def __init__(self, client: Any, *, runs: PostgresParkedRuns | None = None) -> None:
        self._runs = runs or PostgresParkedRuns()
        self._resumer = TemporalParkResumer(client)

    async def parked(self, tenant: str) -> list[str]:
        return await self._runs.find(tenant)

    async def resume(self, workflow_id: str) -> bool:
        return await self._resumer.resume(
            workflow_id, actor=MODEL_PATH_PROBE_ACTOR, actor_type=INTAKE_ACTOR
        )


@dataclass(frozen=True)
class ParkResumeResult:
    target: str
    parked: tuple[str, ...]
    resumed: tuple[str, ...]

    @property
    def message(self) -> str:
        if not self.parked:
            return f"{self.target} has no run parked on the model path, nothing to resume"
        if not self.resumed:
            runs = ", ".join(self.parked)
            return f"{self.target} has already left the model path park on {runs}"
        return f"{self.target} resumed {', '.join(self.resumed)}"


async def resume_parked_runs(
    *,
    tenant: str,
    issue_key: str = "",
    workflow_id: str = "",
    actor: str,
    runs: Any,
    resumer: Any,
) -> ParkResumeResult:
    parked = await runs.find(tenant, issue_key=issue_key, workflow_id=workflow_id)
    resumed = [
        parked_id
        for parked_id in parked
        if await resumer.resume(parked_id, actor=actor, actor_type=OPERATOR_ACTOR)
    ]
    return ParkResumeResult(issue_key or workflow_id, tuple(parked), tuple(resumed))


async def run_resume_parked_runs(
    *, issue_key: str = "", workflow_id: str = "", actor: str, settings: Any = None
) -> ParkResumeResult:
    from database.engine import create_platform_engine
    from scripts.config import get_settings, to_host_hostport, to_host_url

    from services import temporal_client

    resolved = settings or get_settings()
    engine = create_platform_engine(to_host_url(resolved.database_url, resolved))
    try:
        client = await temporal_client.connect(
            to_host_hostport(resolved.temporal_host, resolved), resolved
        )
        return await resume_parked_runs(
            tenant=resolved.tenant.strip(),
            issue_key=issue_key,
            workflow_id=workflow_id,
            actor=actor,
            runs=PostgresParkedRuns(engine),
            resumer=TemporalParkResumer(client),
        )
    finally:
        await engine.dispose()
