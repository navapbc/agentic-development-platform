"""Temporal activity that reads a run's effective limits, its configured limits plus its grants."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity


@dataclass
class RunLimits:
    max_cost_usd: float
    max_llm_calls: int
    max_tokens: int


@dataclass
class RunLimitsRequest:
    execution_id: str
    configured: RunLimits


@activity.defn
async def read_run_limits(req: RunLimitsRequest) -> RunLimits:
    from scripts.config import get_settings

    from services.orchestration.limit_grants import PostgresLimitGrants, effective_value

    tenant = get_settings().tenant.strip()
    store = PostgresLimitGrants()

    async def _effective(limit_name: str, configured: float) -> float:
        return await effective_value(
            store, tenant, limit_name, configured, execution_id=req.execution_id
        )

    return RunLimits(
        max_cost_usd=await _effective("IMPLEMENTATION_MAX_COST_USD", req.configured.max_cost_usd),
        max_llm_calls=int(
            await _effective("IMPLEMENTATION_MAX_LLM_CALLS", req.configured.max_llm_calls)
        ),
        max_tokens=int(await _effective("IMPLEMENTATION_MAX_TOKENS", req.configured.max_tokens)),
    )


@dataclass
class TenantBudgetState:
    spent: bool
    usage: str = ""
    spend: float = 0.0


@dataclass
class TenantBudgetRequest:
    execution_id: str


@activity.defn
async def read_tenant_budget(req: TenantBudgetRequest) -> TenantBudgetState:
    """Return whether the tenant's period budget, with its period grants, is spent."""
    from scripts.config import get_settings

    from services.orchestration.limit_grants import PostgresLimitGrants
    from services.orchestration.tenant_jobs import read_period_budget

    budget = await read_period_budget(get_settings(), grants=PostgresLimitGrants())
    if budget is None or not budget.spent:
        return TenantBudgetState(spent=False)
    return TenantBudgetState(spent=True, usage=budget.usage, spend=budget.spend)


@dataclass
class PeriodAlertThread:
    channel: str = ""
    ts: str = ""
    reason: str = ""
    """Why the park posts at the top level, or empty for a reply in the period alert's thread."""


@activity.defn
async def period_alert_thread(req: TenantBudgetRequest) -> PeriodAlertThread:
    """Return the Slack post of the spent period's alert, posting the alert once when no intake
    pass has yet."""
    from scripts.config import get_settings

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.orchestration.limit_grants import PostgresLimitGrants
    from services.orchestration.tenant_jobs import (
        ensure_period_alert,
        read_period_budget,
        send_period_alert,
    )

    settings = get_settings()
    grants = PostgresLimitGrants()
    budget = await read_period_budget(settings, grants=grants)
    if budget is None or not budget.spent:
        return PeriodAlertThread(reason="the period budget was not spent when the run parked")
    post = await ensure_period_alert(
        PostgresSideEffectLedger(),
        settings.tenant.strip(),
        budget,
        lambda spent: send_period_alert(settings, spent, grants=grants),
    )
    return PeriodAlertThread(channel=post.channel, ts=post.ts, reason=post.reason)
