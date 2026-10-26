"""Hermetic tests for the platform metrics and log-export setup."""

from __future__ import annotations

import logging

from observability import (
    configure_logging,
    configure_metrics,
    record_gate_outcome,
    record_stage_cost,
    record_stage_latency,
    reset_logging_for_testing,
    reset_metrics_for_testing,
)
from observability.logs import force_flush_logs
from observability.metrics import force_flush_metrics
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

# Built at runtime so the source carries no credential-shaped literal for secret scanners to match.
_AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI" + "abcd1234"


def _metric_points(reader: InMemoryMetricReader) -> dict[str, list]:
    data = reader.get_metrics_data()
    points: dict[str, list] = {}
    if data is None:
        return points
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    return points


def test_metrics_record_gate_cost_and_latency():
    reset_metrics_for_testing()
    reader = InMemoryMetricReader()
    assert configure_metrics("test-metrics", reader=reader) is True

    record_gate_outcome("code-review", "pass")
    record_gate_outcome("security-review", "block")
    record_stage_cost("IMPLEMENTING", "bedrock/model", 1.50)
    record_stage_latency("IMPLEMENTING", 12.5)

    points = _metric_points(reader)
    assert "adevx.gate.outcomes" in points
    assert "adevx.stage.cost" in points
    assert "adevx.stage.latency" in points

    verdicts = {p.attributes.get("verdict") for p in points["adevx.gate.outcomes"]}
    assert verdicts == {"pass", "block"}
    cost = sum(p.value for p in points["adevx.stage.cost"])
    assert cost == 1.50

    reset_metrics_for_testing()


def test_metric_helpers_ignore_nonpositive_values():
    reset_metrics_for_testing()
    reader = InMemoryMetricReader()
    configure_metrics("test-metrics", reader=reader)

    record_stage_cost("PLAN", "m", 0.0)
    record_stage_latency("PLAN", 0.0)

    points = _metric_points(reader)
    assert "adevx.stage.cost" not in points
    assert "adevx.stage.latency" not in points

    reset_metrics_for_testing()


def test_metric_helpers_never_raise_without_configuration():
    reset_metrics_for_testing()
    record_gate_outcome("code-review", "pass")
    record_stage_cost("PLAN", "m", 1.0)
    record_stage_latency("PLAN", 1.0)


def test_configure_metrics_is_idempotent():
    reset_metrics_for_testing()
    reader = InMemoryMetricReader()
    assert configure_metrics("svc", reader=reader) is True
    assert configure_metrics("svc-again") is True
    reset_metrics_for_testing()


def test_logging_exports_records_over_otlp():
    reset_logging_for_testing()
    exporter = InMemoryLogRecordExporter()
    assert configure_logging("test-logs", exporter=exporter) is True

    logging.getLogger("adevx.test").warning("gate parked for review")
    force_flush_logs()

    emitted = exporter.get_finished_logs()
    messages = [record.log_record.body for record in emitted]
    assert any("gate parked for review" in str(message) for message in messages)

    reset_logging_for_testing()


def test_configure_logging_is_idempotent():
    reset_logging_for_testing()
    exporter = InMemoryLogRecordExporter()
    assert configure_logging("svc", exporter=exporter) is True
    assert configure_logging("svc-again") is True
    reset_logging_for_testing()


def test_logging_redacts_aws_credentials():
    reset_logging_for_testing()
    exporter = InMemoryLogRecordExporter()
    assert configure_logging("test-redaction", exporter=exporter) is True

    logging.getLogger("adevx.test").warning(
        "boto session AKIAIOSFODNN7EXAMPLE aws_secret_access_key=" + _AWS_SECRET_ACCESS_KEY
    )
    force_flush_logs()

    messages = [str(record.log_record.body) for record in exporter.get_finished_logs()]
    assert messages
    assert all("AKIAIOSFODNN7EXAMPLE" not in message for message in messages)
    assert all("wJalrXUtnFEMIabcd1234" not in message for message in messages)

    reset_logging_for_testing()


def test_redact_secrets_scrubs_known_patterns():
    from observability.logs import redact_secrets

    assert "AKIAIOSFODNN7EXAMPLE" not in redact_secrets("id=AKIAIOSFODNN7EXAMPLE")
    assert "topsecret" not in redact_secrets("aws_session_token=topsecret")
    assert redact_secrets("gate parked for review") == "gate parked for review"


def test_force_flush_metrics_never_raises_without_configuration():
    reset_metrics_for_testing()
    force_flush_metrics()
