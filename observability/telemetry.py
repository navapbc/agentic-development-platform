"""OpenTelemetry tracing setup and helpers for the platform.

Every entry point is best effort. A tracing failure is logged and swallowed so
instrumentation never raises into a workflow, an activity, or a request path.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from opentelemetry import baggage, propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanProcessor
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from observability import attributes as attrs

log = logging.getLogger("observability")

_TRACER_NAME = "adevx"
_configured = False
_provider: TracerProvider | None = None


class ContextStampingSpanProcessor(SpanProcessor):
    """Stamp the platform identifiers from baggage onto every span at start."""

    def on_start(self, span: Span, parent_context: Any | None = None) -> None:
        try:
            items = baggage.get_all(context=parent_context)
            for key in attrs.PLATFORM_KEYS:
                value = items.get(key)
                if value is not None:
                    span.set_attribute(key, str(value))
        except Exception:  # noqa: BLE001 - instrumentation must not raise
            pass

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _composite_propagator() -> CompositePropagator:
    return CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])


def _build_exporter() -> SpanExporter | None:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter()
    except Exception:  # noqa: BLE001
        log.warning("otlp exporter unavailable; spans will not be exported", exc_info=True)
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
        trace.set_tracer_provider(provider)
        propagate.set_global_textmap(_composite_propagator())
        _provider = provider
        _configured = True
        return True
    except Exception:  # noqa: BLE001
        log.warning("tracing setup failed; continuing without telemetry", exc_info=True)
        return False


def reset_for_testing() -> None:
    """Clear the one-time configuration guard. For tests only."""
    global _configured, _provider
    _configured = False
    _provider = None


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
