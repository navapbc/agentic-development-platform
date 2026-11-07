"""Source-control backend selection per repository, from repositories.yaml."""

from __future__ import annotations

from collections.abc import Callable

from adapters.interfaces.git_host import GitHost
from scripts.config import Settings, get_settings

from services.integration.credentials import ConfigError
from services.integration.github.git_auth import NO_AUTH, GitAuth
from services.integration.repositories import (
    GITHUB,
    LOCAL,
    RepositoryRegistry,
    load_repository_registry,
)

__all__ = ["GITHUB", "LOCAL", "backend_for", "build_git_auth_for", "build_git_host"]


def backend_for(
    repo: str, settings: Settings | None = None, *, registry: RepositoryRegistry | None = None
) -> str:
    """Return the source-control backend repositories.yaml configures for repo."""
    return (registry or load_repository_registry(settings)).backend_for(repo)


def build_git_host(settings: Settings | None = None) -> GitHost:
    """Return a GitHost that routes each repository to its configured backend."""
    from adapters.real.routing_git_host import RoutingGitHost

    resolved = settings or get_settings()
    registry = load_repository_registry(resolved)

    def github() -> GitHost:
        from adapters.real.git_host import RealGitHost

        return RealGitHost.from_settings(resolved)

    def local() -> GitHost:
        from adapters.real.local_git_host import LocalGitHost

        return LocalGitHost.from_settings(resolved)

    return RoutingGitHost(registry.backend_for, {GITHUB: github, LOCAL: local})


def build_git_auth_for(settings: Settings | None = None) -> Callable[[str], GitAuth]:
    """Return a per-repository git credential lookup.

    A local repository's remote is a path and authenticates nothing. An absent GitHub config
    resolves to no credential as well, because `build_git_host` already fails on it and
    reporting it twice would only move the error to a caller that never reads a remote.
    """
    resolved = settings or get_settings()
    registry = load_repository_registry(resolved)
    github_resolver = None
    if registry.names(GITHUB):
        from services.integration.github.credentials import build_github_credential_resolver

        try:
            github_resolver = build_github_credential_resolver(resolved)
        except ConfigError:
            github_resolver = None

    def auth_for(repo: str) -> GitAuth:
        repository = registry.get(repo)
        if github_resolver is None or repository is None or repository.backend != GITHUB:
            return NO_AUTH
        cred = github_resolver.resolve(repo)
        return GitAuth(cred.token, cred.git_base_url) if cred is not None else NO_AUTH

    return auth_for
