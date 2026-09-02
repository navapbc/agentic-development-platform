"""End-to-end exit proof that one execution is traceable and its accuracy is measurable.

Joins the observability and evaluation guarantees over a single execution id. The
committed gate is hermetic. It stamps a connected trace across every delivery stage
into an in-memory span exporter, reconciles stubbed gateway generations against a
seeded usage row, loads a platform prompt from a stubbed store by its library
version, and reproduces both the review-kernel comparison and the context-accuracy
consolidation from their committed inputs. A live-gated section exercises the same
trace emission and prompt round trip against the running collector and Langfuse when
``P7_LIVE`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from observability import (
    attributes as attrs,
)
from observability import (
    bind_execution,
    configure_tracing,
    force_flush,
    reset_for_testing,
    stage_span,
)
from observability.gateway import (
    langfuse_request_metadata,
    langfuse_session_url,
    reconcile_usage,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from services.orchestration.usage_rollup import BudgetCaps, UsageRow, build_rollup
from services.prompts.library import PROMPTS
from services.prompts.store import LoadedPrompt, load_prompt, set_store
from services.repository_intelligence.eval.consolidate import consolidate, load_scorecards
from services.review.eval.harness import (
    baseline_stage_reviewer,
    offline_fixture_reviewer,
    run_comparison,
)

EXECUTION_ID = "exec-p7-exit"
TASK_ID = "AGENTDEV-1"
LANGFUSE_HOST = "http://localhost:3000"
LANGFUSE_PROJECT = "agentic-dev-platform"

_STAGES = (
    ("plan", "plan", None),
    ("implementation", "implementation", None),
    ("gate.code", "review", "code"),
    ("ci", "ci", None),
    ("repair", "repair", None),
    ("policy", "policy", None),
    ("pull_request", "pr", None),
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_GOLD = _REPO_ROOT / "services/review/eval/gold/review-gates.jsonl"
_CONTEXT_SCORECARDS = _REPO_ROOT / "services/repository_intelligence/eval/scorecards"


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    reset_for_testing()
    memory = InMemorySpanExporter()
    assert configure_tracing("p7-exit", exporter=memory) is True
    yield memory
    reset_for_testing()


def test_execution_trace_connects_every_stage(exporter: InMemorySpanExporter) -> None:
    with bind_execution(execution_id=EXECUTION_ID, task_id=TASK_ID), stage_span(
        "execution", stage="execution"
    ):
        for name, stage, gate in _STAGES:
            with stage_span(name, stage=stage, gate=gate):
                pass

    force_flush()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    root_span = spans["execution"]

    for name, stage, _gate in _STAGES:
        child = spans[name]
        assert child.parent is not None
        assert child.parent.span_id == root_span.context.span_id
        assert child.context.trace_id == root_span.context.trace_id
        assert child.attributes[attrs.EXECUTION_ID] == EXECUTION_ID
        assert child.attributes[attrs.TASK_ID] == TASK_ID
        assert child.attributes[attrs.STAGE] == stage

    recorded_stages = {spans[name].attributes[attrs.STAGE] for name, _s, _g in _STAGES}
    assert recorded_stages == {"plan", "implementation", "review", "ci", "repair", "policy", "pr"}


def test_gateway_generations_reconcile_with_usage_metrics() -> None:
    generations = [
        {
            **langfuse_request_metadata(
                execution_id=EXECUTION_ID,
                task_id=TASK_ID,
                stage="planning",
                prompt_version="planning/v1",
            ),
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "cost_usd": 0.20,
        },
        {
            **langfuse_request_metadata(
                execution_id=EXECUTION_ID,
                task_id=TASK_ID,
                stage="implementation",
                prompt_version="coding-agent/v1",
            ),
            "prompt_tokens": 500,
            "completion_tokens": 300,
            "cost_usd": 1.50,
        },
    ]

    for generation in generations:
        assert generation["session_id"] == EXECUTION_ID
        assert generation["version"] in {"planning/v1", "coding-agent/v1"}

    reconciliation = reconcile_usage(
        generations, prompt_tokens=600, completion_tokens=340, cost_usd=1.70
    )
    assert reconciliation.ok is True
    assert reconciliation.prompt_token_diff == 0
    assert reconciliation.completion_token_diff == 0


def test_rollup_ties_spend_to_the_langfuse_session() -> None:
    rows = [
        UsageRow(
            execution_id=EXECUTION_ID,
            stage="PLANNING",
            task_id=TASK_ID,
            model_id="planning-model",
            prompt_tokens=100,
            completion_tokens=40,
            total_tokens=140,
            llm_calls=1,
            cost_usd=0.20,
        ),
        UsageRow(
            execution_id=EXECUTION_ID,
            stage="IMPLEMENTING",
            task_id=TASK_ID,
            model_id="impl-model",
            prompt_tokens=500,
            completion_tokens=300,
            total_tokens=800,
            llm_calls=5,
            cost_usd=1.50,
        ),
    ]
    session_url = langfuse_session_url(LANGFUSE_HOST, LANGFUSE_PROJECT, EXECUTION_ID)
    rollup = build_rollup(rows, caps=BudgetCaps(), langfuse_session_url=session_url)

    assert rollup.execution_id == EXECUTION_ID
    assert rollup.langfuse_session_url.endswith(f"/sessions/{EXECUTION_ID}")
    assert rollup.totals.cost_usd == 1.70
    assert rollup.budget.counted_cost_usd == 1.50
    assert rollup.budget.breached is False


def test_platform_prompt_loads_by_library_version() -> None:
    name = "plan-review"
    definition = PROMPTS[name]

    class _LangfuseStub:
        def get(self, prompt_name: str) -> LoadedPrompt:
            target = PROMPTS[prompt_name]
            return LoadedPrompt(target.name, target.version, target.text, "langfuse")

    set_store(_LangfuseStub())
    try:
        loaded = load_prompt(name)
    finally:
        set_store(None)

    assert loaded.source == "langfuse"
    assert loaded.version == definition.version
    assert loaded.name == name


def _stable_review(card: dict) -> dict:
    return {key: value for key, value in card.items() if key not in {"generated_at", "commit"}}


def test_review_kernel_scorecard_reproduces() -> None:
    reviewers = {
        "keyword-baseline": baseline_stage_reviewer,
        "rebar-kernel": offline_fixture_reviewer,
    }
    first = run_comparison(_REVIEW_GOLD, reviewers, model_tier="offline-fixture")
    second = run_comparison(_REVIEW_GOLD, reviewers, model_tier="offline-fixture")

    assert _stable_review(first) == _stable_review(second)
    assert first["metric"] == "verdict_accuracy"
    overall = first["comparison"]["overall"]
    assert 0.0 <= overall["keyword-baseline"] <= 1.0
    assert 0.0 <= overall["rebar-kernel"] <= 1.0


def test_context_accuracy_scorecard_reproduces() -> None:
    cards = load_scorecards(_CONTEXT_SCORECARDS)
    first = consolidate(cards)
    second = consolidate(cards)

    stable_first = {k: v for k, v in first.items() if k != "generated_at"}
    stable_second = {k: v for k, v in second.items() if k != "generated_at"}
    assert stable_first == stable_second
    assert first["summary"]["repos"] == 6
    assert first["definition_of_done"] is True


@pytest.mark.skipif(not os.environ.get("P7_LIVE"), reason="live-gated exit proof")
def test_live_trace_and_prompt_roundtrip() -> None:
    from services.prompts.store import LangfusePromptStore, seed_prompts

    reset_for_testing()
    try:
        assert configure_tracing("p7-exit-live") is True
        with bind_execution(execution_id=EXECUTION_ID, task_id=TASK_ID), stage_span(
            "execution", stage="execution"
        ):
            for name, stage, gate in _STAGES:
                with stage_span(name, stage=stage, gate=gate):
                    pass
        force_flush()
    finally:
        reset_for_testing()

    seed_prompts()
    loaded = LangfusePromptStore().get("plan-review")
    assert loaded.source == "langfuse"
    assert loaded.version == PROMPTS["plan-review"].version
