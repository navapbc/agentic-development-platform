"""Tests for the LOG_LEVEL setting that configures each service's root logger."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from scripts.config import Settings, configured_log_level


def test_log_level_is_normalized_to_a_logging_level_name():
    assert Settings(log_level=" debug ").log_level == "DEBUG"


def test_unknown_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(log_level="verbose")


def test_configured_log_level_reads_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")

    assert configured_log_level() == "WARNING"
