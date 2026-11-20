"""Per-execution cost, token, and budget rollup over the usage_metrics table.

Reduces the recorded per-stage usage rows into per-stage, per-gate, and per-model
spend, breaks out the repair spend, compares the implementation and repair spend
against the implementation budget caps to surface the breach reason with the same
precedence the draft-pull-request workflow applies, and carries the Langfuse
session link for the execution. Each row carries the tenant recorded on its
execution, so the rollups also group by tenant.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from database.models.schema import executions, usage_metrics

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"

_UNKNOWN_MODEL = "unknown"


def _is_repair(stage: str) -> bool:
    return "REPAIR" in stage.upper()


def _is_implementation(stage: str) -> bool:
    return "IMPLEMENT" in stage.upper()


def _is_gate(stage: str) -> bool:
    upper = stage.upper()
    return upper.endswith("_REVIEW") or upper == "ACCEPTANCE"


@dataclass(frozen=True)
class UsageRow:
    execution_id: str
    stage: str
    task_id: str = ""
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
    tenant: str = ""


@dataclass(frozen=True)
class Spend:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class StageSpend:
    stage: str
    spend: Spend


@dataclass(frozen=True)
class ModelSpend:
    model_id: str
    spend: Spend


@dataclass(frozen=True)
class BudgetCaps:
    max_cost_usd: float = 5.0
    max_llm_calls: int = 200
    max_tokens: int = 2_000_000

    @classmethod
    def from_settings(cls, settings: object) -> BudgetCaps:
        return cls(
            max_cost_usd=float(getattr(settings, "implementation_max_cost_usd", 5.0)),
            max_llm_calls=int(getattr(settings, "implementation_max_llm_calls", 200)),
            max_tokens=int(getattr(settings, "implementation_max_tokens", 2_000_000)),
        )


@dataclass(frozen=True)
class BudgetStatus:
    max_cost_usd: float
    max_llm_calls: int
    max_tokens: int
    counted_cost_usd: float
    counted_llm_calls: int
    counted_tokens: int
    breach_reason: str

    @property
    def breached(self) -> bool:
        return bool(self.breach_reason)


@dataclass(frozen=True)
class ExecutionCostRollup:
    execution_id: str
    task_id: str
    totals: Spend
    by_stage: tuple[StageSpend, ...]
    by_model: tuple[ModelSpend, ...]
    gates: tuple[StageSpend, ...]
    repair: Spend
    budget: BudgetStatus
    langfuse_session_url: str
    tenant: str = ""


@dataclass(frozen=True)
class TenantCostRollup:
    tenant: str
    execution_count: int
    totals: Spend
    by_model: tuple[ModelSpend, ...]


def _accumulate(rows: Iterable[UsageRow]) -> Spend:
    prompt = completion = cache_read = cache_write = total = calls = tools = 0
    cost = elapsed = 0.0
    for row in rows:
        prompt += row.prompt_tokens
        completion += row.completion_tokens
        cache_read += row.cache_read_tokens
        cache_write += row.cache_write_tokens
        total += row.total_tokens
        calls += row.llm_calls
        tools += row.tool_calls
        cost += row.cost_usd
        elapsed += row.elapsed_seconds
    return Spend(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=total,
        llm_calls=calls,
        tool_calls=tools,
        cost_usd=cost,
        elapsed_seconds=elapsed,
    )


def _budget_status(counted: Spend, caps: BudgetCaps) -> BudgetStatus:
    breach = ""
    billable_tokens = max(0, counted.total_tokens - counted.cache_read_tokens)
    if counted.llm_calls > caps.max_llm_calls:
        breach = LLM_CALLS_EXCEEDED
    elif billable_tokens > caps.max_tokens:
        breach = TOKEN_BUDGET_EXCEEDED
    elif counted.cost_usd > caps.max_cost_usd:
        breach = COST_BUDGET_EXCEEDED
    return BudgetStatus(
        max_cost_usd=caps.max_cost_usd,
        max_llm_calls=caps.max_llm_calls,
        max_tokens=caps.max_tokens,
        counted_cost_usd=counted.cost_usd,
        counted_llm_calls=counted.llm_calls,
        counted_tokens=billable_tokens,
        breach_reason=breach,
    )


def build_rollup(
    rows: Sequence[UsageRow],
    *,
    caps: BudgetCaps,
    langfuse_session_url: str = "",
) -> ExecutionCostRollup:
    execution_id = rows[0].execution_id if rows else ""
    task_id = next((row.task_id for row in rows if row.task_id), "")
    tenant = next((row.tenant for row in rows if row.tenant), "")

    stages = sorted({row.stage for row in rows})
    by_stage = tuple(
        StageSpend(stage, _accumulate(row for row in rows if row.stage == stage))
        for stage in stages
    )

    by_model = _by_model(rows)

    gates = tuple(item for item in by_stage if _is_gate(item.stage))
    repair = _accumulate(row for row in rows if _is_repair(row.stage))
    counted = _accumulate(
        row for row in rows if _is_implementation(row.stage) or _is_repair(row.stage)
    )

    return ExecutionCostRollup(
        execution_id=execution_id,
        task_id=task_id,
        totals=_accumulate(rows),
        by_stage=by_stage,
        by_model=by_model,
        gates=gates,
        repair=repair,
        budget=_budget_status(counted, caps),
        langfuse_session_url=langfuse_session_url,
        tenant=tenant,
    )


def _by_model(rows: Sequence[UsageRow]) -> tuple[ModelSpend, ...]:
    models = sorted({row.model_id or _UNKNOWN_MODEL for row in rows})
    return tuple(
        ModelSpend(
            model,
            _accumulate(row for row in rows if (row.model_id or _UNKNOWN_MODEL) == model),
        )
        for model in models
    )


def rows_by_execution(rows: Iterable[UsageRow]) -> dict[str, list[UsageRow]]:
    """Group usage rows by execution, in the order each execution first appears."""
    grouped: dict[str, list[UsageRow]] = {}
    for row in rows:
        grouped.setdefault(row.execution_id, []).append(row)
    return grouped


def build_tenant_rollups(rows: Sequence[UsageRow]) -> tuple[TenantCostRollup, ...]:
    """Roll usage rows up per tenant, sorted by tenant, with untenanted rows under ``""``."""
    tenants = sorted({row.tenant for row in rows})
    rollups: list[TenantCostRollup] = []
    for tenant in tenants:
        tenant_rows = [row for row in rows if row.tenant == tenant]
        rollups.append(
            TenantCostRollup(
                tenant=tenant,
                execution_count=len({row.execution_id for row in tenant_rows}),
                totals=_accumulate(tenant_rows),
                by_model=_by_model(tenant_rows),
            )
        )
    return tuple(rollups)


def _spend_to_dict(spend: Spend) -> dict[str, float | int]:
    return {
        "prompt_tokens": spend.prompt_tokens,
        "completion_tokens": spend.completion_tokens,
        "cache_read_tokens": spend.cache_read_tokens,
        "cache_write_tokens": spend.cache_write_tokens,
        "total_tokens": spend.total_tokens,
        "llm_calls": spend.llm_calls,
        "tool_calls": spend.tool_calls,
        "cost_usd": round(spend.cost_usd, 6),
        "elapsed_seconds": round(spend.elapsed_seconds, 3),
    }


def rollup_to_dict(rollup: ExecutionCostRollup) -> dict[str, object]:
    return {
        "execution_id": rollup.execution_id,
        "task_id": rollup.task_id,
        "tenant": rollup.tenant or None,
        "langfuse_session_url": rollup.langfuse_session_url,
        "totals": _spend_to_dict(rollup.totals),
        "by_stage": [
            {"stage": item.stage, **_spend_to_dict(item.spend)} for item in rollup.by_stage
        ],
        "by_model": [
            {"model_id": item.model_id, **_spend_to_dict(item.spend)} for item in rollup.by_model
        ],
        "gates": [{"stage": item.stage, **_spend_to_dict(item.spend)} for item in rollup.gates],
        "repair": _spend_to_dict(rollup.repair),
        "budget": {
            "max_cost_usd": rollup.budget.max_cost_usd,
            "max_llm_calls": rollup.budget.max_llm_calls,
            "max_tokens": rollup.budget.max_tokens,
            "counted_cost_usd": round(rollup.budget.counted_cost_usd, 6),
            "counted_llm_calls": rollup.budget.counted_llm_calls,
            "counted_tokens": rollup.budget.counted_tokens,
            "breach_reason": rollup.budget.breach_reason,
            "breached": rollup.budget.breached,
        },
    }


def execution_summary_to_dict(rollup: ExecutionCostRollup) -> dict[str, object]:
    """One execution's row in a multi-execution rollup: identity, tenant, totals, and breach."""
    return {
        "execution_id": rollup.execution_id,
        "task_id": rollup.task_id,
        "tenant": rollup.tenant or None,
        "langfuse_session_url": rollup.langfuse_session_url,
        "totals": _spend_to_dict(rollup.totals),
        "breach_reason": rollup.budget.breach_reason,
    }


def tenant_rollup_to_dict(rollup: TenantCostRollup) -> dict[str, object]:
    return {
        "tenant": rollup.tenant or None,
        "executions": rollup.execution_count,
        "totals": _spend_to_dict(rollup.totals),
        "by_model": [
            {"model_id": item.model_id, **_spend_to_dict(item.spend)} for item in rollup.by_model
        ],
    }


async def read_usage_rows(
    engine: AsyncEngine, execution_id: str = "", *, tenant: str = ""
) -> list[UsageRow]:
    """Read recorded usage rows with the tenant recorded on each row's execution.

    An empty execution id reads every execution, and an empty tenant reads every tenant.
    usage_metrics has no foreign key to executions, so the join is outer and a row
    without an execution record, or from a run that predates the tenant column, carries
    an empty tenant.
    """
    from sqlalchemy import select

    stmt = select(usage_metrics, executions.c.tenant.label("execution_tenant")).select_from(
        usage_metrics.outerjoin(
            executions, usage_metrics.c.execution_id == executions.c.execution_id
        )
    )
    if execution_id:
        stmt = stmt.where(usage_metrics.c.execution_id == execution_id)
    if tenant:
        stmt = stmt.where(executions.c.tenant == tenant)
    stmt = stmt.order_by(usage_metrics.c.execution_id, usage_metrics.c.stage)
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        return [
            UsageRow(
                execution_id=row["execution_id"],
                stage=row["stage"],
                task_id=row["task_id"] or "",
                model_id=row["model_id"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                total_tokens=row["total_tokens"],
                llm_calls=row["llm_calls"],
                tool_calls=row["tool_calls"],
                cost_usd=float(row["cost_usd"]),
                elapsed_seconds=float(row["elapsed_seconds"]),
                tenant=row["execution_tenant"] or "",
            )
            for row in result.mappings()
        ]


def _stage_norm(name: str) -> str:
    return name.strip().upper().replace("-", "_")


def backfill_zeroed_stages(
    rows: Sequence[UsageRow], generations: Sequence[Mapping[str, Any]]
) -> list[UsageRow]:
    """Refill zeroed per-stage usage from the true usage in Langfuse generations.

    A stage whose in-process row rebar returned as zero (the review gates, whose token
    usage rebar does not report) is refilled from the summed Langfuse generations that
    carry the same stage. The Langfuse generation name is the hyphenated stage, so it
    normalizes onto the uppercase in-process stage. Rows that already hold usage stay
    untouched, so implementation and repair spend is never double counted.
    """
    from dataclasses import replace

    by_stage: dict[str, dict[str, Any]] = {}
    for generation in generations:
        stage = _stage_norm(str(generation.get("stage") or ""))
        if not stage:
            continue
        bucket = by_stage.setdefault(
            stage,
            {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0, "model_id": None},
        )
        bucket["prompt"] += int(generation.get("prompt_tokens", 0) or 0)
        bucket["completion"] += int(generation.get("completion_tokens", 0) or 0)
        bucket["cost"] += float(generation.get("cost_usd", 0.0) or 0.0)
        bucket["calls"] += 1
        if bucket["model_id"] is None and generation.get("model_id"):
            bucket["model_id"] = generation.get("model_id")

    merged: list[UsageRow] = []
    for row in rows:
        zeroed = row.prompt_tokens == 0 and row.completion_tokens == 0 and row.cost_usd == 0.0
        bucket = by_stage.get(_stage_norm(row.stage)) if zeroed else None
        if bucket is None:
            merged.append(row)
            continue
        prompt = int(bucket["prompt"])
        completion = int(bucket["completion"])
        merged.append(
            replace(
                row,
                model_id=row.model_id or bucket["model_id"],
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                llm_calls=row.llm_calls or int(bucket["calls"]),
                cost_usd=float(bucket["cost"]),
            )
        )
    return merged
