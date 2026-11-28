"""Bind each activity's tenant into the telemetry baggage, so its model calls and spans carry it.

A tenant's activity worker and review worker serve only their tenant's task queues, so every
activity they run belongs to the tenant their `TENANT` setting names. The review worker also reads
the tenant from the execution record of the run the activity input names, and refuses a run that
another tenant recorded. The baggage tenant becomes the `tenant:<tenant>` tag of each LiteLLM
request and Langfuse trace, and the `tenant` attribute of each span.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from observability import bind_execution
from temporalio.exceptions import ApplicationError
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput
from temporalio.worker import Interceptor as WorkerInterceptor

log = logging.getLogger(__name__)

Lookup = Callable[[str], Awaitable[str]]
_CACHE_SIZE = 512


def execution_id_of(args: Sequence[Any]) -> str:
    """Return the execution id of the activity input, at the top level or in its context."""
    for arg in args:
        context = arg.get("context") if isinstance(arg, dict) else getattr(arg, "context", None)
        for holder in (arg, context):
            if holder is None:
                continue
            value = (
                holder.get("execution_id")
                if isinstance(holder, dict)
                else getattr(holder, "execution_id", "")
            )
            if value:
                return str(value)
    return ""


async def recorded_execution_tenant(execution_id: str) -> str:
    from services.orchestration.temporal_activities.persistence import execution_tenant

    return await execution_tenant(execution_id)


class TenantAttributionInterceptor(WorkerInterceptor):
    """Bind the worker's tenant, or the tenant a run's execution record names, to each activity.

    With a lookup, the recorded tenant of the run wins and the worker's tenant is the fallback
    for an activity whose run has no record yet. A worker bound to a tenant refuses, without
    retry, an activity whose run another tenant recorded. A lookup that fails leaves the activity
    attributed to the fallback, because an unavailable record must not fail a review.
    """

    def __init__(self, tenant: str = "", lookup: Lookup | None = None) -> None:
        self._tenant = tenant.strip()
        self._lookup = lookup
        self._recorded: OrderedDict[str, str] = OrderedDict()

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _AttributedActivityInbound(next, self)

    async def tenant_for(self, execution_id: str) -> str:
        if self._lookup is None or not execution_id:
            return self._tenant
        if execution_id in self._recorded:
            self._recorded.move_to_end(execution_id)
            return self._refuse_other_tenant(execution_id, self._recorded[execution_id])
        try:
            recorded = await self._lookup(execution_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the tenant of execution %s (%s)", execution_id, exc)
            return self._tenant
        if not recorded:
            return self._tenant
        self._recorded[execution_id] = recorded
        while len(self._recorded) > _CACHE_SIZE:
            self._recorded.popitem(last=False)
        return self._refuse_other_tenant(execution_id, recorded)

    def _refuse_other_tenant(self, execution_id: str, recorded: str) -> str:
        if self._tenant and recorded != self._tenant:
            from services.orchestration.tenant_pinning import TENANT_MISMATCH

            raise ApplicationError(
                f"execution {execution_id} belongs to tenant {recorded}, and this worker serves "
                f"tenant {self._tenant}",
                type=TENANT_MISMATCH,
                non_retryable=True,
            )
        return recorded


class _AttributedActivityInbound(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, owner: TenantAttributionInterceptor
    ) -> None:
        super().__init__(next)
        self._owner = owner

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        tenant = await self._owner.tenant_for(execution_id_of(input.args))
        if not tenant:
            return await super().execute_activity(input)
        with bind_execution(tenant=tenant):
            return await super().execute_activity(input)
