"""Config-driven selection of the source-control backend, defaulting to GitHub."""

from __future__ import annotations

from adapters.interfaces.git_host import GitHost
from scripts.config import Settings, get_settings

from services.integration.credentials import ConfigError

GITHUB = "github"
LOCAL = "local"
SOURCE_CONTROL_BACKENDS = (GITHUB, LOCAL)


def resolve_source_control_backend_name(settings: Settings | None = None) -> str:
    """Return the configured source-control backend name, defaulting to GitHub when unset."""
    resolved = settings or get_settings()
    name = (resolved.source_control_backend or GITHUB).strip().lower()
    if name not in SOURCE_CONTROL_BACKENDS:
        raise ConfigError(
            f"unknown source_control_backend {name!r}; "
            f"expected one of {', '.join(SOURCE_CONTROL_BACKENDS)}"
        )
    return name


def build_git_host(settings: Settings | None = None) -> GitHost:
    """Return the source-control backend, selected by config.

    Defaults to GitHub so a deployment that configures no source-control backend
    behaves exactly as before.
    """
    resolved = settings or get_settings()
    name = resolve_source_control_backend_name(resolved)
    if name == GITHUB:
        from adapters.real.git_host import RealGitHost

        return RealGitHost.from_settings(resolved)
    from adapters.real.local_git_host import LocalGitHost

    return LocalGitHost.from_settings(resolved)
