"""Config-driven selection of the source-control backend, defaulting to GitHub."""

from __future__ import annotations

from collections.abc import Callable

from adapters.interfaces.git_host import GitHost
from scripts.config import Settings, get_settings

from services.integration.credentials import ConfigError
from services.integration.github.git_auth import NO_AUTH, GitAuth

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


def build_git_auth_for(settings: Settings | None = None) -> Callable[[str], GitAuth]:
    """Return a per-repository git credential lookup for the configured backend.

    The local backend addresses remotes by filesystem path and authenticates nothing, so it
    resolves to no credential. An absent GitHub config resolves to no credential as well,
    because `build_git_host` already fails on it and reporting it twice would only move the
    error to a caller that never reads a remote.
    """
    resolved = settings or get_settings()
    if resolve_source_control_backend_name(resolved) != GITHUB:
        return lambda repo: NO_AUTH

    from services.integration.github.credentials import build_github_credential_resolver

    try:
        github_resolver = build_github_credential_resolver(resolved)
    except ConfigError:
        return lambda repo: NO_AUTH

    def auth_for(repo: str) -> GitAuth:
        cred = github_resolver.resolve(repo)
        return GitAuth(cred.token, cred.git_base_url) if cred is not None else NO_AUTH

    return auth_for
