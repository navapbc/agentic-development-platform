"""Backend-neutral source-control helpers shared across the run lifecycle.

The source-control backend selection lives in ``services.integration.github.factory``
as ``build_git_host``. This module adds the repo allowlist that the intake and
materialization paths need without binding them to the GitHub credential file, so
a deployment that targets only a local git folder needs no ``github.yaml``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.config import Settings, get_settings

from services.integration.github.factory import GITHUB, resolve_source_control_backend_name


def source_control_repos(settings: Settings | None = None) -> set[str]:
    """Return the allowlisted repositories for the configured source-control backend."""
    resolved = settings or get_settings()
    if resolve_source_control_backend_name(resolved) == GITHUB:
        from services.integration.github.credentials import allowed_repos

        return set(allowed_repos(resolved))
    return _local_repos(resolved)


def _local_repos(settings: Settings) -> set[str]:
    declared = {
        name.strip() for name in settings.source_control_local_repos.split(",") if name.strip()
    }
    if declared:
        return declared
    root = settings.source_control_local_root.strip()
    if not root:
        return set()
    base = Path(root)
    if not base.is_dir():
        return set()
    return {child.name for child in base.iterdir() if (child / ".git").is_dir()}
