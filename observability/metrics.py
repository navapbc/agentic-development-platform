"""OpenTelemetry metrics setup and the platform instruments.

Every entry point is best effort. A metrics failure is logged and swallowed so
instrumentation never raises into a workflow, an activity, or a request path.
The instruments cover the review gate outcomes, the per-stage latency, and the
per-stage cost, so those pillars are queryable alongside the traces.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from opentelemetry.sdk.resources import Resource

log = logging.getLogger("observability")

_METER_NAME = "adevx"
_configured = False
_provider: Any | None = None
_gate_outcomes: Any | None = None
_stage_latency: Any | None = None
_stage_cost: Any | None = None


def _build_metric_reader() -> Any | None:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        return PeriodicExportingMetricReader(OTLPMetricExporter())
    except Exception:  # noqa: BLE001
        log.warning("otlp metric exporter unavailable; metrics will not be exported", exc_info=True)
        return None


def configure_metrics(service_name: str, *, reader: Any | None = None) -> bool:
    """Install a global meter provider once. Returns True when metrics are active."""
    global _configured, _provider, _gate_outcomes, _stage_latency, _stage_cost
    if _configured:
        return True
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider

        metric_reader = reader if reader is not None else _build_metric_reader()
        if metric_reader is None:
            return False
        provider = MeterProvider(
            resource=Resource.create({"service.name": service_name}),
            metric_readers=[metric_reader],
        )
        metrics.set_meter_provider(provider)
        meter = provider.get_meter(_METER_NAME)
        _gate_outcomes = meter.create_counter(
            "adevx.gate.outcomes",
            unit="1",
            description="Count of review gate outcomes by gate and verdict.",
        )
        _stage_latency = meter.create_histogram(
            "adevx.stage.latency",
            unit="s",
            description="Per-stage elapsed time in seconds.",
        )
        _stage_cost = meter.create_counter(
            "adevx.stage.cost",
            unit="usd",
            description="Per-stage model cost in US dollars.",
        )
        _provider = provider
        _configured = True
        return True
    except Exception:  # noqa: BLE001
        log.warning("metrics setup failed; continuing without metrics", exc_info=True)
        return False


def record_gate_outcome(gate: str, verdict: str) -> None:
    """Count one review gate outcome. Never raises."""
    if _gate_outcomes is None:
        return
    with contextlib.suppress(Exception):
        _gate_outcomes.add(1, {"gate": gate, "verdict": verdict})


def record_stage_latency(stage: str, seconds: float) -> None:
    """Record one per-stage latency sample in seconds. Never raises."""
    if _stage_latency is None or seconds <= 0:
        return
    with contextlib.suppress(Exception):
        _stage_latency.record(float(seconds), {"stage": stage})


def record_stage_cost(stage: str, model_id: str | None, cost_usd: float) -> None:
    """Add one per-stage cost sample in dollars. Never raises."""
    if _stage_cost is None or cost_usd <= 0:
        return
    with contextlib.suppress(Exception):
        _stage_cost.add(float(cost_usd), {"stage": stage, "model_id": model_id or "unknown"})


def force_flush_metrics(timeout_millis: int = 5000) -> None:
    """Flush pending metrics through the configured provider. Never raises."""
    with contextlib.suppress(Exception):
        if _provider is not None:
            _provider.force_flush(timeout_millis)


def reset_metrics_for_testing() -> None:
    """Clear the one-time configuration guard. For tests only."""
    global _configured, _provider, _gate_outcomes, _stage_latency, _stage_cost
    _configured = False
    _provider = None
    _gate_outcomes = None
    _stage_latency = None
    _stage_cost = None
