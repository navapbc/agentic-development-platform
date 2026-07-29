"""Unit tests for the multi-repo RIS registry."""

from __future__ import annotations

from pathlib import Path

from services.repository_intelligence.context_builder.registry import (
    RepositoryIntelligenceRegistry,
    RepoSpec,
)


def _registry(recorder):
    def builder(root, *, commit=None, cbm_binary=None):
        recorder.append((str(root), commit, cbm_binary))
        return object()

    specs = [
        RepoSpec("service", Path("/repos/service"), "sha-a", "/bin/cbm"),
        RepoSpec("greenfield", Path("/repos/greenfield"), None, None),
    ]
    return RepositoryIntelligenceRegistry(specs, builder=builder)


def test_registry_lists_registered_repos():
    assert _registry([]).repos() == {"service", "greenfield"}


def test_registry_routes_to_the_matching_repo():
    recorder: list[tuple[str, str | None, str | None]] = []
    registry = _registry(recorder)

    registry.service("service")
    registry.service("greenfield")

    assert recorder == [
        ("/repos/service", "sha-a", "/bin/cbm"),
        ("/repos/greenfield", None, None),
    ]


def test_registry_caches_each_service():
    registry = _registry([])
    first = registry.service("service")
    assert first is registry.service("service")
    assert first is not registry.service("greenfield")


def test_registry_returns_none_for_unregistered_repo():
    assert _registry([]).service("missing") is None
