"""Backend-neutral source-control helpers shared across the run lifecycle."""

from __future__ import annotations

from scripts.config import Settings, get_settings

from services.integration.repositories import load_repository_registry


def source_control_repos(settings: Settings | None = None) -> set[str]:
    """Return every repository repositories.yaml configures, whatever its backend."""
    return load_repository_registry(settings or get_settings()).names()
