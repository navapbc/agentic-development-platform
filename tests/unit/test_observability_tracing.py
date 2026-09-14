"""Hermetic tests for the platform tracing schema and span stamping."""

from __future__ import annotations

from typing import cast

import pytest
from observability import (
    attributes as attrs,
)
from observability import (
    bind_execution,
    configure_tracing,
    force_flush,
    get_tracer,
    record_generation,
    reset_for_testing,
    stage_span,
    temporal_interceptors,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    reset_for_testing()
    memory = InMemorySpanExporter()
    assert configure_tracing("test-service", exporter=memory) is True
    yield memory
    reset_for_testing()


def _by_name(exporter: InMemorySpanExporter) -> dict[str, object]:
    force_flush()
    return {span.name: span for span in exporter.get_finished_spans()}


def test_configure_tracing_is_idempotent(exporter: InMemorySpanExporter) -> None:
    assert configure_tracing("test-service-again") is True


def test_stage_span_carries_the_pinned_attributes(exporter: InMemorySpanExporter) -> None:
    with stage_span(
        "review",
        stage="review",
        gate="code",
        execution_id="exec-1",
        task_id="TASK-1",
        model_id="claude-x",
        prompt_version="v3",
    ):
        pass

    span = _by_name(exporter)["review"]
    assert span.attributes[attrs.STAGE] == "review"
    assert span.attributes[attrs.GATE] == "code"
    assert span.attributes[attrs.EXECUTION_ID] == "exec-1"
    assert span.attributes[attrs.TASK_ID] == "TASK-1"
    assert span.attributes[attrs.MODEL_ID] == "claude-x"
    assert span.attributes[attrs.PROMPT_VERSION] == "v3"


def test_bound_identifiers_stamp_every_span(exporter: InMemorySpanExporter) -> None:
    tracer = get_tracer()
    with (
        bind_execution(execution_id="exec-9", task_id="TASK-9"),
        tracer.start_as_current_span("plan"),
        tracer.start_as_current_span("implement"),
    ):
        pass

    spans = _by_name(exporter)
    for name in ("plan", "implement"):
        assert spans[name].attributes[attrs.EXECUTION_ID] == "exec-9"
        assert spans[name].attributes[attrs.TASK_ID] == "TASK-9"


def test_bound_execution_stamps_the_langfuse_session_join(exporter: InMemorySpanExporter) -> None:
    tracer = get_tracer()
    with (
        bind_execution(
            execution_id="exec-lf",
            task_id="TASK-lf",
            stage="plan-review",
            prompt_version="v2",
        ),
        tracer.start_as_current_span("gen"),
    ):
        pass

    span = cast(ReadableSpan, _by_name(exporter)["gen"])
    attributes = span.attributes or {}
    assert attributes[attrs.LANGFUSE_SESSION_ID] == "exec-lf"
    assert attributes[f"{attrs.LANGFUSE_TRACE_METADATA_PREFIX}stage"] == "plan-review"
    assert attributes[f"{attrs.LANGFUSE_TRACE_METADATA_PREFIX}task_id"] == "TASK-lf"
    assert attributes[f"{attrs.LANGFUSE_TRACE_METADATA_PREFIX}prompt_version"] == "v2"


def test_genai_exporter_forwards_only_generation_spans() -> None:
    from observability.telemetry import _GenAISpanExporter
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _Inner(SpanExporter):
        def __init__(self) -> None:
            self.exported: list[object] = []

        def export(self, spans):
            self.exported.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    class _Span:
        def __init__(self, attributes: dict[str, object]) -> None:
            self.attributes = attributes

    inner = _Inner()
    exporter = _GenAISpanExporter(inner)
    generation = _Span({attrs.GEN_AI_REQUEST_MODEL: "claude-x"})
    other = _Span({attrs.STAGE: "plan-review"})

    result = exporter.export([generation, other])

    assert result is SpanExportResult.SUCCESS
    assert inner.exported == [generation]


def test_genai_exporter_skips_the_inner_export_without_generations() -> None:
    from observability.telemetry import _GenAISpanExporter
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _Inner(SpanExporter):
        def __init__(self) -> None:
            self.calls = 0

        def export(self, spans):
            self.calls += 1
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    class _Span:
        attributes: dict[str, object] = {"stage": "plan-review"}

    inner = _Inner()
    result = _GenAISpanExporter(inner).export([_Span()])

    assert result is SpanExportResult.SUCCESS
    assert inner.calls == 0


def test_build_langfuse_exporter_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from observability.telemetry import _build_langfuse_exporter, _GenAISpanExporter

    for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert _build_langfuse_exporter() is None

    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    exporter = _build_langfuse_exporter()
    assert isinstance(exporter, _GenAISpanExporter)


def test_instrument_llm_tracing_is_idempotent() -> None:
    from observability import instrument_llm_tracing

    assert instrument_llm_tracing() is True
    assert instrument_llm_tracing() is True


def test_spans_form_one_connected_tree(exporter: InMemorySpanExporter) -> None:
    with (
        bind_execution(execution_id="exec-tree", task_id="TASK-tree"),
        stage_span("plan", stage="plan"),
        stage_span("gate.code", stage="review", gate="code"),
    ):
        pass

    spans = _by_name(exporter)
    root = spans["plan"]
    child = spans["gate.code"]
    assert child.parent is not None
    assert child.parent.span_id == root.context.span_id
    assert child.context.trace_id == root.context.trace_id
    assert child.attributes[attrs.EXECUTION_ID] == "exec-tree"


def test_record_generation_sets_genai_attributes(exporter: InMemorySpanExporter) -> None:
    with stage_span("llm.invoke", stage="implementation") as span:
        record_generation(
            span,
            request_model="classification-summary",
            response_model="bedrock/claude-x",
            input_tokens=12,
            output_tokens=34,
        )

    span = _by_name(exporter)["llm.invoke"]
    assert span.attributes[attrs.GEN_AI_REQUEST_MODEL] == "classification-summary"
    assert span.attributes[attrs.GEN_AI_RESPONSE_MODEL] == "bedrock/claude-x"
    assert span.attributes[attrs.GEN_AI_USAGE_INPUT_TOKENS] == 12
    assert span.attributes[attrs.GEN_AI_USAGE_OUTPUT_TOKENS] == 34
    assert span.attributes[attrs.MODEL_ID] == "bedrock/claude-x"


def test_stage_span_never_raises_without_tracing() -> None:
    reset_for_testing()
    with stage_span("noop", stage="plan") as span:
        assert span is None or span is not None
    reset_for_testing()


def test_stage_span_propagates_a_body_exception(exporter: InMemorySpanExporter) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), stage_span("explode", stage="plan"):
        raise Boom()

    span = _by_name(exporter)["explode"]
    assert span.status.status_code.name == "ERROR"


def test_temporal_interceptors_available() -> None:
    interceptors = temporal_interceptors()
    assert len(interceptors) == 2


def test_extract_identifiers_reads_dataclass_and_dict_inputs() -> None:
    from dataclasses import dataclass

    from observability.temporal import _extract_identifiers

    @dataclass
    class Params:
        execution_id: str
        task_id: str

    assert _extract_identifiers([Params("exec-1", "TASK-1")]) == {
        "execution_id": "exec-1",
        "task_id": "TASK-1",
    }
    assert _extract_identifiers([{"execution_id": "exec-2"}]) == {"execution_id": "exec-2"}
    assert _extract_identifiers(["no-identifiers"]) == {}
