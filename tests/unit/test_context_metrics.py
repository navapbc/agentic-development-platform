"""Hermetic tests for context-package accuracy metrics and traced attributes."""

from __future__ import annotations

import pytest
from observability import configure_tracing, force_flush, reset_for_testing
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from services.repository_intelligence.context_metrics import context_accuracy_attributes


def _package() -> dict:
    return {
        "focus": "TaskStore",
        "symbols": [
            {"name": "TaskStore", "provenance": {"source": "scip", "confidence": 0.9}},
            {"name": "save", "provenance": {"source": "scip", "confidence": 0.8}},
        ],
        "references": [
            {"symbol": "save", "provenance": {"source": "scip", "confidence": 1.0}},
        ],
        "callers": [
            {"caller": {}, "callee": {}, "provenance": {"source": "scip", "confidence": 0.6}},
        ],
        "callees": [],
        "implementations": [],
        "related_tests": [],
        "dependencies": [],
    }


def test_accuracy_attributes_count_relationships():
    attrs = context_accuracy_attributes(_package())
    assert attrs["context.count.symbols"] == 2
    assert attrs["context.count.references"] == 1
    assert attrs["context.count.callers"] == 1
    assert attrs["context.count.callees"] == 0
    assert attrs["context.relationship_total"] == 4


def test_mean_confidence_is_the_average_over_scored_relationships():
    attrs = context_accuracy_attributes(_package())
    # (0.9 + 0.8 + 1.0 + 0.6) / 4
    assert attrs["context.mean_confidence"] == 0.825
    assert attrs["context.scored_relationships"] == 4


def test_completeness_is_the_populated_category_fraction():
    attrs = context_accuracy_attributes(_package())
    # symbols, references, callers populated = 3 of 7 categories
    assert attrs["context.completeness"] == round(3 / 7, 4)


def test_empty_package_is_zero_without_raising():
    attrs = context_accuracy_attributes({})
    assert attrs["context.relationship_total"] == 0
    assert attrs["context.mean_confidence"] == 0.0
    assert attrs["context.completeness"] == 0.0


@pytest.mark.asyncio
async def test_gather_repo_context_stamps_accuracy_attributes(monkeypatch):
    from services.orchestration.temporal_activities import planning

    reset_for_testing()
    memory = InMemorySpanExporter()
    assert configure_tracing("context-accuracy-test", exporter=memory) is True

    async def fake_fetch(repo, commit, focus, detail=""):
        return _package()

    monkeypatch.setattr(planning, "fetch_context_package", fake_fetch)

    await planning.gather_repo_context(
        planning.ContextRequest(
            repo="agentic-demo-service",
            commit="abc",
            focus="TaskStore",
            execution_id="exec-1",
            task_id="TASK-1",
        )
    )

    force_flush()
    span = next(s for s in memory.get_finished_spans() if s.name == "context.gather")
    assert span.attributes["execution_id"] == "exec-1"
    assert span.attributes["context.mean_confidence"] == 0.825
    assert span.attributes["context.completeness"] == round(3 / 7, 4)
    assert span.attributes["context.count.symbols"] == 2
    assert "context.warm_latency_ms" in span.attributes
    reset_for_testing()
