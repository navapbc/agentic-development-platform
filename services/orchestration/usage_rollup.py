"""Per-execution cost, token, and budget rollup over the usage_metrics table.

Reduces the recorded per-stage usage rows into per-stage, per-gate, and per-model
spend, breaks out the repair spend, compares the implementation and repair spend
against the implementation budget caps to surface the breach reason with the same
precedence the draft-pull-request workflow applies, and carries the Langfuse
session link for the execution.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from database.models.schema import usage_metrics

COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"

_UNKNOWN_MODEL = "unknown"


def _is_repair(stage: str) -> bool:
    return "REPAIR" in stage.upper()


def _is_implementation(stage: str) -> bool:
    return "IMPLEMENT" in stage.upper()


def _is_gate(stage: str) -> bool:
    return stage.upper().endswith("_REVIEW")


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
    if counted.llm_calls > caps.max_llm_calls:
        breach = LLM_CALLS_EXCEEDED
    elif counted.total_tokens > caps.max_tokens:
        breach = TOKEN_BUDGET_EXCEEDED
    elif counted.cost_usd > caps.max_cost_usd:
        breach = COST_BUDGET_EXCEEDED
    return BudgetStatus(
        max_cost_usd=caps.max_cost_usd,
        max_llm_calls=caps.max_llm_calls,
        max_tokens=caps.max_tokens,
        counted_cost_usd=counted.cost_usd,
        counted_llm_calls=counted.llm_calls,
        counted_tokens=counted.total_tokens,
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

    stages = sorted({row.stage for row in rows})
    by_stage = tuple(
        StageSpend(stage, _accumulate(row for row in rows if row.stage == stage))
        for stage in stages
    )

    models = sorted({row.model_id or _UNKNOWN_MODEL for row in rows})
    by_model = tuple(
        ModelSpend(
            model,
            _accumulate(row for row in rows if (row.model_id or _UNKNOWN_MODEL) == model),
        )
        for model in models
    )

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
    )


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


async def read_usage_rows(engine: object, execution_id: str) -> list[UsageRow]:
    """Read the recorded usage rows for one execution from the usage_metrics table."""
    from sqlalchemy import select

    stmt = select(usage_metrics).where(usage_metrics.c.execution_id == execution_id)
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
            )
            for row in result.mappings()
        ]
