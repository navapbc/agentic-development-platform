"""The otel-collector redacts secret-shaped fields on every telemetry pipeline."""

from __future__ import annotations

from pathlib import Path

import yaml
from observability import attributes as attrs

_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "observability"
    / "otel"
    / "otel-collector-config.yaml"
)

_REQUIRED_REDACTIONS = {
    "aws.secret_access_key",
    "aws.access_key_id",
    "authorization",
    "x-api-key",
    "cookie",
    "jira.api_token",
    "github.token",
    "langfuse.secret_key",
}


def _load() -> dict:
    return yaml.safe_load(_CONFIG.read_text())


def test_redact_processor_covers_secret_shaped_keys() -> None:
    config = _load()
    actions = config["processors"]["attributes/redact"]["actions"]
    deleted = {action["key"] for action in actions if action["action"] == "delete"}
    assert deleted >= _REQUIRED_REDACTIONS


def test_redact_processor_is_on_trace_and_log_pipelines() -> None:
    config = _load()
    pipelines = config["service"]["pipelines"]
    for name in ("traces", "logs"):
        assert "attributes/redact" in pipelines[name]["processors"]


def test_redact_does_not_drop_pinned_platform_keys() -> None:
    config = _load()
    actions = config["processors"]["attributes/redact"]["actions"]
    deleted = {action["key"] for action in actions if action["action"] == "delete"}
    assert not (set(attrs.PLATFORM_KEYS) & deleted)


def test_all_pipelines_batch_before_export() -> None:
    config = _load()
    for name, pipeline in config["service"]["pipelines"].items():
        assert pipeline["processors"][-1] == "batch", name
