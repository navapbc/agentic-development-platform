"""Tests for per-integration mode parsing and defaults."""

from __future__ import annotations

import pytest
from services.integration.credentials import ConfigError
from services.integration.integrations import (
    HYBRID,
    POLLING,
    WEBHOOK,
    parse_integrations_config,
)


def test_modes_parse_and_default_to_polling() -> None:
    config = parse_integrations_config({"modes": {"pr_closed": WEBHOOK, "jira_intake": HYBRID}})

    assert config.mode("pr_closed") == WEBHOOK
    assert config.mode("jira_intake") == HYBRID
    assert config.mode("ci_status") == POLLING


def test_webhook_enabled_for_webhook_and_hybrid_only() -> None:
    config = parse_integrations_config({"modes": {"a": WEBHOOK, "b": HYBRID, "c": POLLING}})

    assert config.webhook_enabled("a") is True
    assert config.webhook_enabled("b") is True
    assert config.webhook_enabled("c") is False
    assert config.webhook_enabled("missing") is False


def test_unknown_mode_fails_closed() -> None:
    with pytest.raises(ConfigError, match="must be one of"):
        parse_integrations_config({"modes": {"pr_closed": "sometimes"}})


def test_modes_must_be_a_mapping() -> None:
    with pytest.raises(ConfigError, match="modes must be a mapping"):
        parse_integrations_config({"modes": ["pr_closed"]})
