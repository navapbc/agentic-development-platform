"""Find and resume the runs parked at a reached limit when an administrator grants more of it.

A parked run waits on the ``resume_limit_park`` update naming its park key. The resumer reads the
key from the run's ``suspension_state`` query, so a grant that reaches a run after it left the
park, or a second grant for the same park, resumes nothing. A run grant resumes the park of its
execution and a period grant resumes every tenant run parked on the tenant model budget.
"""

from __future__ import annotations

from typing import Any

from services.orchestration.limit_grants import RUN_SCOPE, LimitGrant
from services.orchestration.model_path_parks import ParkedRunFinder, resume_each, resume_park
from services.orchestration.suspension import RESUME_LIMIT_PARK_UPDATE, SuspendReason

GRANT_ACTOR_TYPE = "limit_grant"
_AWAITING_HUMAN = "AWAITING_HUMAN"


def limit_parked_runs_query(tenant: str, *, limit_name: str, execution_id: str = "") -> Any:
    from database.models.schema import executions, suspended_flows
    from sqlalchemy import select

    from services.orchestration.execution_tenant import tenant_executions

    stmt = (
        select(suspended_flows.c.workflow_id)
        .join(executions, executions.c.workflow_id == suspended_flows.c.workflow_id)
        .where(suspended_flows.c.reason == SuspendReason.LIMIT_REACHED.value)
        .where(suspended_flows.c.detail == limit_name)
        .where(executions.c.state == _AWAITING_HUMAN)
        .where(tenant_executions(tenant))
    )
    if execution_id:
        stmt = stmt.where(executions.c.execution_id == execution_id)
    return stmt.distinct().order_by(suspended_flows.c.workflow_id)


class PostgresLimitParkedRuns(ParkedRunFinder):
    async def find(self, tenant: str, *, limit_name: str, execution_id: str = "") -> list[str]:
        return await self._workflow_ids(
            limit_parked_runs_query(tenant, limit_name=limit_name, execution_id=execution_id)
        )


class TemporalLimitParkResumer:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def resume(
        self,
        workflow_id: str,
        *,
        limit_name: str,
        actor: str,
        actor_type: str = GRANT_ACTOR_TYPE,
    ) -> bool:
        """Resume the run's park on ``limit_name``, returning whether this call resumed it."""
        return await resume_park(
            self._client,
            workflow_id,
            reason=SuspendReason.LIMIT_REACHED,
            update=RESUME_LIMIT_PARK_UPDATE,
            actor=actor,
            actor_type=actor_type,
            detail=limit_name,
        )


class LimitParkResumption:
    def __init__(self, runs: Any, resumer: Any) -> None:
        self._runs = runs
        self._resumer = resumer

    async def __call__(self, grant: LimitGrant) -> list[str]:
        execution_id = grant.execution_id if grant.scope == RUN_SCOPE else ""
        parked = await self._runs.find(
            grant.tenant, limit_name=grant.limit_name, execution_id=execution_id
        )
        return await resume_each(
            parked,
            lambda workflow_id: self._resumer.resume(
                workflow_id, limit_name=grant.limit_name, actor=grant.approver
            ),
        )
