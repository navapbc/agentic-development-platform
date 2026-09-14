"""OpenTelemetry tracing setup and helpers for the platform.

Every entry point is best effort. A tracing failure is logged and swallowed so
instrumentation never raises into a workflow, an activity, or a request path.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from opentelemetry import baggage, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from observability import attributes as attrs

log = logging.getLogger("observability")

_TRACER_NAME = "adevx"
_configured = False
_provider: TracerProvider | None = None
_llm_instrumented = False


class ContextStampingSpanProcessor(SpanProcessor):
    """Stamp the platform identifiers from baggage onto every span at start."""

    def on_start(self, span: Span, parent_context: Any | None = None) -> None:
        try:
            items = baggage.get_all(context=parent_context)
            for key in attrs.PLATFORM_KEYS:
                value = items.get(key)
                if value is not None:
                    span.set_attribute(key, str(value))
            self._stamp_langfuse_join(span, items)
        except Exception:  # noqa: BLE001 - instrumentation must not raise
            pass

    @staticmethod
    def _stamp_langfuse_join(span: Span, items: Mapping[str, Any]) -> None:
        """Map the execution and stage onto Langfuse-recognized span attributes.

        Langfuse groups OTEL observations into a session by ``langfuse.session.id``,
        the same join the proxied path carried as the LiteLLM ``session_id``. The
        stage, task, and prompt version ride as trace metadata so a native review
        generation is attributable to its execution and stage in Langfuse.
        """
        execution_id = items.get(attrs.EXECUTION_ID)
        if execution_id is not None:
            span.set_attribute(attrs.LANGFUSE_SESSION_ID, str(execution_id))
        for key in (attrs.STAGE, attrs.TASK_ID, attrs.PROMPT_VERSION):
            value = items.get(key)
            if value is not None:
                span.set_attribute(f"{attrs.LANGFUSE_TRACE_METADATA_PREFIX}{key}", str(value))

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class _GenAISpanExporter(SpanExporter):
    """Forward only generative-AI spans to the wrapped exporter.

    The platform provider fans every span out to the collector, but Langfuse should
    receive only the model generations, matching the proxied path where only LLM
    calls reach Langfuse. A span qualifies when it carries any ``gen_ai.*``
    attribute, which pydantic-ai's instrumentation sets on each model request.
    """

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    @staticmethod
    def _is_generation(span: Any) -> bool:
        attributes = getattr(span, "attributes", None) or {}
        return any(str(key).startswith(attrs.GEN_AI_PREFIX) for key in attributes)

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        kept = [span for span in spans if self._is_generation(span)]
        if not kept:
            return SpanExportResult.SUCCESS
        return self._inner.export(kept)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _composite_propagator() -> CompositePropagator:
    return CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])


def _build_exporter() -> SpanExporter | None:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter()
    except Exception:  # noqa: BLE001
        log.warning("otlp exporter unavailable; spans will not be exported", exc_info=True)
        return None


def _build_langfuse_exporter() -> SpanExporter | None:
    """A Langfuse OTLP exporter for generative-AI spans, or ``None`` when unconfigured.

    Native review calls bypass the LiteLLM Langfuse callback, so their generations
    reach Langfuse only through pydantic-ai OTEL spans exported here. Returns
    ``None`` without Langfuse credentials or the OTLP HTTP exporter, so the platform
    degrades to collector-only tracing rather than failing.
    """
    host = os.getenv("LANGFUSE_HOST")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    if not (host and public_key and secret_key):
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        endpoint = f"{host.rstrip('/')}/api/public/otel/v1/traces"
        inner = OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": f"Basic {auth}"})
        return _GenAISpanExporter(inner)
    except Exception:  # noqa: BLE001
        log.warning("langfuse otlp exporter unavailable; native spans skip it", exc_info=True)
        return None


def configure_tracing(service_name: str, *, exporter: SpanExporter | None = None) -> bool:
    """Install a global tracer provider once. Returns True when tracing is active."""
    global _configured, _provider
    if _configured:
        return True
    try:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(ContextStampingSpanProcessor())
        span_exporter = exporter if exporter is not None else _build_exporter()
        if span_exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(span_exporter))
        if exporter is None:
            langfuse_exporter = _build_langfuse_exporter()
            if langfuse_exporter is not None:
                provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))
        trace.set_tracer_provider(provider)
        propagate.set_global_textmap(_composite_propagator())
        _provider = provider
        _configured = True
        return True
    except Exception:  # noqa: BLE001
        log.warning("tracing setup failed; continuing without telemetry", exc_info=True)
        return False


def instrument_llm_tracing() -> bool:
    """Emit pydantic-ai agent, LLM, and tool spans through the configured provider.

    Idempotent and best effort. Native review calls are only traced to Langfuse
    when pydantic-ai is instrumented, so the worker installs this at startup rather
    than relying on a per-call side effect deeper in the runner.
    """
    global _llm_instrumented
    if _llm_instrumented:
        return True
    try:
        from pydantic_ai import Agent

        Agent.instrument_all()
        _llm_instrumented = True
        return True
    except Exception:  # noqa: BLE001
        log.warning("pydantic-ai instrumentation unavailable; llm spans not traced", exc_info=True)
        return False


def reset_for_testing() -> None:
    """Clear the one-time configuration guard. For tests only."""
    global _configured, _provider, _llm_instrumented
    _configured = False
    _provider = None
    _llm_instrumented = False


def force_flush(timeout_millis: int = 5000) -> None:
    """Flush pending spans through the configured provider. Never raises."""
    try:
        if _provider is not None:
            _provider.force_flush(timeout_millis)
    except Exception:  # noqa: BLE001
        pass


def get_tracer() -> Tracer:
    if _provider is not None:
        return _provider.get_tracer(_TRACER_NAME)
    return trace.get_tracer(_TRACER_NAME)


def _apply_attributes(span: Span, values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        if value is None:
            continue
        with contextlib.suppress(Exception):
            span.set_attribute(key, value)


@contextlib.contextmanager
def stage_span(
    name: str,
    *,
    stage: str | None = None,
    gate: str | None = None,
    execution_id: str | None = None,
    task_id: str | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Span | None]:
    """Start a span carrying the pinned platform attributes.

    Span setup never raises; when a span cannot be started the block runs with
    ``None``. Exceptions from the wrapped block propagate so instrumentation does
    not swallow a caller's error, and the span records the exception.
    """
    try:
        cm = get_tracer().start_as_current_span(name)
    except Exception:  # noqa: BLE001
        yield None
        return
    with cm as span:
        try:
            _apply_attributes(
                span,
                {
                    attrs.STAGE: stage,
                    attrs.GATE: gate,
                    attrs.EXECUTION_ID: execution_id,
                    attrs.TASK_ID: task_id,
                    attrs.MODEL_ID: model_id,
                    attrs.PROMPT_VERSION: prompt_version,
                },
            )
            if attributes:
                _apply_attributes(span, attributes)
        except Exception:  # noqa: BLE001
            pass
        yield span


def record_generation(
    span: Span | None,
    *,
    request_model: str | None = None,
    response_model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    system: str = "bedrock",
) -> None:
    """Attach GenAI semantic-convention attributes to a span. Never raises."""
    if span is None:
        return
    _apply_attributes(
        span,
        {
            attrs.GEN_AI_SYSTEM: system,
            attrs.GEN_AI_REQUEST_MODEL: request_model,
            attrs.GEN_AI_RESPONSE_MODEL: response_model,
            attrs.GEN_AI_USAGE_INPUT_TOKENS: input_tokens,
            attrs.GEN_AI_USAGE_OUTPUT_TOKENS: output_tokens,
            attrs.MODEL_ID: response_model or request_model,
        },
    )


def temporal_interceptors() -> list[Any]:
    """Return the Temporal interceptors that connect and stamp the execution trace."""
    interceptors: list[Any] = []
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor

        from observability.temporal import PlatformTelemetryInterceptor

        interceptors.append(
            TracingInterceptor(tracer=get_tracer(), always_create_workflow_spans=True)
        )
        interceptors.append(PlatformTelemetryInterceptor())
    except Exception:  # noqa: BLE001
        log.warning("temporal tracing interceptors unavailable", exc_info=True)
    return interceptors
