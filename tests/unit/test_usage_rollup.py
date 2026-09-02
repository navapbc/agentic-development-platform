"""Hermetic tests for the per-execution cost, token, and budget rollup."""

from __future__ import annotations

from observability.gateway import langfuse_session_url
from services.orchestration.usage_rollup import (
    COST_BUDGET_EXCEEDED,
    LLM_CALLS_EXCEEDED,
    TOKEN_BUDGET_EXCEEDED,
    BudgetCaps,
    UsageRow,
    build_rollup,
    rollup_to_dict,
)

EXECUTION = "exec-1"


def _rows() -> list[UsageRow]:
    return [
        UsageRow(
            execution_id=EXECUTION,
            stage="PLANNING",
            task_id="TASK-1",
            model_id="planning-model",
            prompt_tokens=100,
            completion_tokens=40,
            total_tokens=140,
            llm_calls=1,
            cost_usd=0.20,
            elapsed_seconds=2.0,
        ),
        UsageRow(
            execution_id=EXECUTION,
            stage="PLAN_REVIEW",
            task_id="TASK-1",
            model_id="review-model",
            prompt_tokens=80,
            completion_tokens=20,
            total_tokens=100,
            llm_calls=2,
            cost_usd=0.30,
            elapsed_seconds=1.5,
        ),
        UsageRow(
            execution_id=EXECUTION,
            stage="IMPLEMENTING",
            task_id="TASK-1",
            model_id="impl-model",
            prompt_tokens=500,
            completion_tokens=300,
            total_tokens=800,
            llm_calls=5,
            tool_calls=9,
            cost_usd=1.50,
            elapsed_seconds=30.0,
        ),
        UsageRow(
            execution_id=EXECUTION,
            stage="REPAIRING",
            task_id="TASK-1",
            model_id="impl-model",
            total_tokens=200,
            llm_calls=2,
            cost_usd=0.60,
            elapsed_seconds=8.0,
        ),
    ]


def test_totals_sum_every_row():
    rollup = build_rollup(_rows(), caps=BudgetCaps())
    assert rollup.execution_id == EXECUTION
    assert rollup.task_id == "TASK-1"
    assert rollup.totals.total_tokens == 1240
    assert rollup.totals.llm_calls == 10
    assert rollup.totals.cost_usd == 2.60
    assert rollup.totals.tool_calls == 9


def test_by_stage_groups_and_sorts():
    rollup = build_rollup(_rows(), caps=BudgetCaps())
    stages = [item.stage for item in rollup.by_stage]
    assert stages == ["IMPLEMENTING", "PLANNING", "PLAN_REVIEW", "REPAIRING"]
    impl = next(item for item in rollup.by_stage if item.stage == "IMPLEMENTING")
    assert impl.spend.cost_usd == 1.50


def test_by_model_collapses_shared_model():
    rollup = build_rollup(_rows(), caps=BudgetCaps())
    models = {item.model_id: item.spend for item in rollup.by_model}
    assert set(models) == {"planning-model", "review-model", "impl-model"}
    assert models["impl-model"].total_tokens == 1000
    assert models["impl-model"].cost_usd == 2.10


def test_gates_only_include_review_stages():
    rollup = build_rollup(_rows(), caps=BudgetCaps())
    assert [item.stage for item in rollup.gates] == ["PLAN_REVIEW"]


def test_repair_spend_broken_out():
    rollup = build_rollup(_rows(), caps=BudgetCaps())
    assert rollup.repair.total_tokens == 200
    assert rollup.repair.llm_calls == 2
    assert rollup.repair.cost_usd == 0.60


def test_budget_counts_only_implementation_and_repair():
    rollup = build_rollup(_rows(), caps=BudgetCaps())
    assert rollup.budget.counted_tokens == 1000
    assert rollup.budget.counted_llm_calls == 7
    assert rollup.budget.counted_cost_usd == 2.10
    assert rollup.budget.breach_reason == ""
    assert rollup.budget.breached is False


def test_cost_breach_reported():
    rollup = build_rollup(_rows(), caps=BudgetCaps(max_cost_usd=1.0))
    assert rollup.budget.breach_reason == COST_BUDGET_EXCEEDED
    assert rollup.budget.breached is True


def test_llm_calls_breach_takes_precedence_over_cost():
    rollup = build_rollup(
        _rows(), caps=BudgetCaps(max_cost_usd=1.0, max_llm_calls=1, max_tokens=1)
    )
    assert rollup.budget.breach_reason == LLM_CALLS_EXCEEDED


def test_token_breach_takes_precedence_over_cost():
    rollup = build_rollup(_rows(), caps=BudgetCaps(max_cost_usd=1.0, max_tokens=1))
    assert rollup.budget.breach_reason == TOKEN_BUDGET_EXCEEDED


def test_gate_spend_excluded_from_budget_count():
    rows = [
        UsageRow(execution_id=EXECUTION, stage="CODE_REVIEW", llm_calls=500, cost_usd=100.0),
    ]
    rollup = build_rollup(rows, caps=BudgetCaps())
    assert rollup.budget.counted_llm_calls == 0
    assert rollup.budget.breach_reason == ""


def test_unknown_model_bucket():
    rows = [UsageRow(execution_id=EXECUTION, stage="IMPLEMENTING", total_tokens=10)]
    rollup = build_rollup(rows, caps=BudgetCaps())
    assert [item.model_id for item in rollup.by_model] == ["unknown"]


def test_langfuse_session_url_carried_and_serialized():
    url = langfuse_session_url("http://localhost:3000", "agentic-dev-platform", EXECUTION)
    assert url == "http://localhost:3000/project/agentic-dev-platform/sessions/exec-1"
    rollup = build_rollup(_rows(), caps=BudgetCaps(), langfuse_session_url=url)
    payload = rollup_to_dict(rollup)
    assert payload["langfuse_session_url"] == url
    assert payload["budget"]["counted_cost_usd"] == 2.10
    assert {"stage", "cost_usd", "total_tokens"} <= set(payload["by_stage"][0])


def test_langfuse_session_url_empty_when_incomplete():
    assert langfuse_session_url("", "agentic-dev-platform", EXECUTION) == ""
    assert langfuse_session_url("http://localhost:3000", "", EXECUTION) == ""
    assert langfuse_session_url("http://localhost:3000", "agentic-dev-platform", "") == ""


def test_empty_rows_yield_empty_rollup():
    rollup = build_rollup([], caps=BudgetCaps())
    assert rollup.execution_id == ""
    assert rollup.by_stage == ()
    assert rollup.totals.total_tokens == 0
    assert rollup.budget.breach_reason == ""


def test_backfill_refills_zeroed_review_stage():
    from services.orchestration.usage_rollup import backfill_zeroed_stages

    rows = [
        UsageRow(
            execution_id=EXECUTION,
            stage="IMPLEMENTING",
            model_id="impl-model",
            prompt_tokens=500,
            completion_tokens=300,
            total_tokens=800,
            llm_calls=5,
            cost_usd=1.50,
        ),
        UsageRow(
            execution_id=EXECUTION,
            stage="CODE_REVIEW",
            model_id=None,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            llm_calls=0,
            cost_usd=0.0,
        ),
    ]
    generations = [
        {"stage": "code-review", "model_id": "review-model",
         "prompt_tokens": 1200, "completion_tokens": 300, "cost_usd": 0.45},
        {"stage": "code-review", "model_id": "review-model",
         "prompt_tokens": 800, "completion_tokens": 100, "cost_usd": 0.25},
        {"stage": "implementing", "model_id": "impl-model",
         "prompt_tokens": 9999, "completion_tokens": 9999, "cost_usd": 99.0},
    ]

    merged = {row.stage: row for row in backfill_zeroed_stages(rows, generations)}

    review = merged["CODE_REVIEW"]
    assert review.prompt_tokens == 2000
    assert review.completion_tokens == 400
    assert review.total_tokens == 2400
    assert review.llm_calls == 2
    assert review.cost_usd == 0.70
    assert review.model_id == "review-model"

    impl = merged["IMPLEMENTING"]
    assert impl.prompt_tokens == 500
    assert impl.cost_usd == 1.50


def test_backfill_leaves_rows_without_matching_generations():
    from services.orchestration.usage_rollup import backfill_zeroed_stages

    rows = [
        UsageRow(execution_id=EXECUTION, stage="SECURITY_REVIEW", prompt_tokens=0,
                 completion_tokens=0, total_tokens=0, cost_usd=0.0),
    ]
    merged = backfill_zeroed_stages(rows, [])
    assert merged[0].prompt_tokens == 0
    assert merged[0].cost_usd == 0.0
