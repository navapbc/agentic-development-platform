"""Unit tests for the multi-repo RIS registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.repository_intelligence.context_builder import registry as registry_module
from services.repository_intelligence.context_builder.registry import (
    RepositoryIntelligenceRegistry,
    RepoSpec,
)
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.languages import Language


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
    assert _registry([]).repos() == {("", "service"), ("", "greenfield")}


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


def test_registry_rebuilds_service_when_language_changes(monkeypatch):
    builds: list[object] = []

    def builder(root, *, commit=None, cbm_binary=None):
        instance = object()
        builds.append(instance)
        return instance

    registry = RepositoryIntelligenceRegistry(
        [RepoSpec("service", Path("/repos/service"))], builder=builder
    )
    languages = iter([Language.UNKNOWN, Language.PYTHON, Language.PYTHON])
    monkeypatch.setattr(registry_module, "primary_language", lambda root: next(languages))

    first = registry.service("service")
    second = registry.service("service")
    third = registry.service("service")

    assert first is not second
    assert second is third
    assert len(builds) == 2


def test_registry_keys_services_by_tenant_and_passes_each_tenants_index_roots():
    built: list[tuple[str, object]] = []

    def builder(root, *, commit=None, cbm_binary=None, index_roots=None) -> Any:
        built.append((str(root), index_roots))
        return object()

    adp_roots = IndexRoots(Path("/idx/scip/adp"), Path("/idx/zoekt/adp"))
    beta_roots = IndexRoots(Path("/idx/scip/beta"), Path("/idx/zoekt/beta"))
    registry = RepositoryIntelligenceRegistry(
        [
            RepoSpec(
                "web", Path("/tenants/adp/repositories/web"), tenant="adp", index_roots=adp_roots
            ),
            RepoSpec(
                "web", Path("/tenants/beta/repositories/web"), tenant="beta", index_roots=beta_roots
            ),
        ],
        builder=builder,
    )

    adp = registry.service("web", "adp")
    beta = registry.service("web", "beta")

    assert adp is not beta
    assert registry.service("web") is None
    assert built == [
        ("/tenants/adp/repositories/web", adp_roots),
        ("/tenants/beta/repositories/web", beta_roots),
    ]


def _any_builder(root, **_options) -> Any:
    return object()


def test_replace_specs_keeps_unchanged_services_and_drops_changed_ones():
    registry = RepositoryIntelligenceRegistry(
        [RepoSpec("svc", Path("/repos/svc"), "sha-a"), RepoSpec("old", Path("/repos/old"))],
        builder=_any_builder,
    )
    kept = registry.service("svc")
    registry.service("old")

    registry.replace_specs(
        [RepoSpec("svc", Path("/repos/svc"), "sha-a"), RepoSpec("new", Path("/repos/new"))]
    )

    assert registry.service("svc") is kept
    assert registry.service("old") is None
    assert registry.service("new") is not None

    registry.replace_specs([RepoSpec("svc", Path("/repos/svc"), "sha-b")])
    assert registry.service("svc") is not kept
