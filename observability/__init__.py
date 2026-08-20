"""Platform telemetry package: OpenTelemetry tracing helpers and the pinned span schema."""

from __future__ import annotations

from observability import attributes
from observability.context import bind_execution, current_identifiers
from observability.telemetry import (
    ContextStampingSpanProcessor,
    configure_tracing,
    force_flush,
    get_tracer,
    record_generation,
    reset_for_testing,
    stage_span,
    temporal_interceptors,
)

__all__ = [
    "attributes",
    "bind_execution",
    "current_identifiers",
    "ContextStampingSpanProcessor",
    "configure_tracing",
    "force_flush",
    "get_tracer",
    "record_generation",
    "reset_for_testing",
    "stage_span",
    "temporal_interceptors",
]
