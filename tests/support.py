"""Shared test helpers."""

from __future__ import annotations

from scripts.config import Settings


def hermetic_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]
