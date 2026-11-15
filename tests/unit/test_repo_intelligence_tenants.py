"""Unit tests for the per-tenant repository registries of repository intelligence."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from services.integration.tenant_config import DirectorySource
from services.repository_intelligence import tenants
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.tenant_tokens import derive_token
from services.repository_intelligence.tenants import (
    PLATFORM_TENANT,
    TenantRegistries,
    configured_tokens,
    home_tenant,
    migrate_home_indexes,
    served_tenants,
)
from tests.support import configure_repositories, hermetic_settings

ENV_REPOSITORIES = "repositories:\n  platform-svc:\n    index: true\n"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _bundle(root: Path, tenant: str, repositories: str | None) -> Path:
    directory = root / "tenants" / tenant
    directory.mkdir(parents=True, exist_ok=True)
    if repositories is not None:
        (directory / "repositories.yaml").write_text(
            "config_version: 1\n" + repositories, encoding="utf-8"
        )
    return directory


def _names(registries: TenantRegistries, tenant: str) -> set[str]:
    registry = registries.registry(tenant)
    assert registry is not None
    return registry.names()


def _registries(tmp_path: Path, monkeypatch, *, tenants_value="adp,beta", tenant="adp", clock=None):
    configure_repositories(monkeypatch, tmp_path / "env", ENV_REPOSITORIES)
    settings = hermetic_settings(
        tenants=tenants_value, tenant=tenant, tenant_config_refresh_seconds=60
    )
    registries = TenantRegistries(
        settings,
        source_factory=lambda name: DirectorySource(tmp_path / "tenants" / name, name),
        clock=clock or _Clock(),
    )
    return settings, registries


def test_served_tenants_prefers_tenants_then_tenant_then_platform_mode():
    assert served_tenants(hermetic_settings(tenants="adp, beta", tenant="beta")) == ["adp", "beta"]
    assert served_tenants(hermetic_settings(tenants="", tenant="adp")) == ["adp"]
    assert served_tenants(hermetic_settings(tenants="", tenant="")) == []


def test_home_tenant_is_tenant_when_served_else_the_first_tenant():
    assert home_tenant(hermetic_settings(tenants="adp,beta", tenant="beta")) == "beta"
    assert home_tenant(hermetic_settings(tenants="adp,beta", tenant="")) == "adp"
    assert home_tenant(hermetic_settings(tenants="", tenant="")) == PLATFORM_TENANT


def test_each_tenant_reads_its_bundle_and_falls_back_to_the_environment(tmp_path, monkeypatch):
    _bundle(tmp_path, "beta", "repositories:\n  beta-web:\n    index: false\n")
    _bundle(tmp_path, "adp", None)
    _, registries = _registries(tmp_path, monkeypatch)

    adp = registries.registry("adp")
    beta = registries.registry("beta")

    assert adp is not None and adp.names() == {"platform-svc"}
    assert beta is not None and beta.names() == {"beta-web"}
    assert adp.location("platform-svc") == "/tenants/adp/repositories/platform-svc"
    assert beta.location("beta-web") == "/tenants/beta/repositories/beta-web"


def test_two_tenants_with_the_same_repository_get_their_own_checkout_and_index(
    tmp_path, monkeypatch
):
    for name in ("adp", "beta"):
        _bundle(tmp_path, name, "repositories:\n  web:\n    index: true\n")
    _, registries = _registries(tmp_path, monkeypatch)

    entries = {(entry.tenant, entry.name): entry for entry in registries.indexed()}

    assert set(entries) == {("adp", "web"), ("beta", "web")}
    assert entries["adp", "web"].root == Path("/tenants/adp/repositories/web")
    assert entries["beta", "web"].root == Path("/tenants/beta/repositories/web")
    default = IndexRoots.default()
    assert entries["adp", "web"].index_roots == IndexRoots(
        default.scip / "adp", default.zoekt / "adp"
    )
    assert entries["beta", "web"].index_roots == IndexRoots(
        default.scip / "beta", default.zoekt / "beta"
    )


def test_a_bundle_refresh_adds_a_repository_after_the_interval(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, "beta", "repositories:\n  beta-web: {}\n")
    _bundle(tmp_path, "adp", None)
    clock = _Clock()
    _, registries = _registries(tmp_path, monkeypatch, clock=clock)
    assert _names(registries, "beta") == {"beta-web"}

    (bundle / "repositories.yaml").write_text(
        "config_version: 1\nrepositories:\n  beta-web: {}\n  beta-api: {}\n", encoding="utf-8"
    )
    clock.now = 30.0
    assert _names(registries, "beta") == {"beta-web"}
    clock.now = 61.0
    assert _names(registries, "beta") == {"beta-web", "beta-api"}


def test_an_invalid_bundle_keeps_the_last_valid_registry(tmp_path, monkeypatch, caplog):
    bundle = _bundle(tmp_path, "beta", "repositories:\n  beta-web: {}\n")
    _bundle(tmp_path, "adp", None)
    clock = _Clock()
    _, registries = _registries(tmp_path, monkeypatch, clock=clock)
    assert _names(registries, "beta") == {"beta-web"}

    (bundle / "repositories.yaml").write_text(
        "config_version: 1\nrepositories:\n  beta-web:\n    unknown_field: 1\n", encoding="utf-8"
    )
    clock.now = 61.0
    with caplog.at_level(logging.ERROR, logger=tenants.__name__):
        registry = registries.registry("beta")

    assert registry is not None and registry.names() == {"beta-web"}
    assert "tenant beta is refused" in caplog.text


def test_an_outdated_config_version_is_refused(tmp_path, monkeypatch):
    directory = _bundle(tmp_path, "beta", None)
    (directory / "repositories.yaml").write_text(
        "config_version: 0\nrepositories:\n  beta-web: {}\n", encoding="utf-8"
    )
    _bundle(tmp_path, "adp", None)
    _, registries = _registries(tmp_path, monkeypatch)

    assert registries.registry("beta") is None
    assert registries.registry("adp") is not None


def test_platform_mode_serves_the_platform_registry_under_the_empty_tenant(tmp_path, monkeypatch):
    _, registries = _registries(tmp_path, monkeypatch, tenants_value="", tenant="")

    assert registries.platform_mode
    assert registries.tenants() == [PLATFORM_TENANT]
    registry = registries.registry(PLATFORM_TENANT)
    assert registry is not None and registry.names() == {"platform-svc"}
    (entry,) = registries.indexed()
    assert entry.tenant == PLATFORM_TENANT and entry.index_roots is None


def test_configured_tokens_derive_each_tenant_token_from_the_key():
    settings = hermetic_settings(tenants="adp,beta", repo_intelligence_token_key="k")

    assert configured_tokens(settings, environ={}) == {
        "adp": derive_token("k", "adp"),
        "beta": derive_token("k", "beta"),
    }


def test_configured_tokens_prefer_the_per_tenant_variable():
    settings = hermetic_settings(tenants="adp,beta", repo_intelligence_token_key="")

    tokens = configured_tokens(settings, environ={"REPO_INTELLIGENCE_TOKEN_BETA": "beta-secret"})

    assert tokens == {"beta": "beta-secret"}


def test_platform_mode_accepts_its_own_token_and_none_without_one():
    with_token = hermetic_settings(tenants="", tenant="", repo_intelligence_token="platform")
    without = hermetic_settings(tenants="", tenant="", repo_intelligence_token="")

    assert configured_tokens(with_token, environ={}) == {PLATFORM_TENANT: "platform"}
    assert configured_tokens(without, environ={}) == {}


def _index(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "marker").write_text(name, encoding="utf-8")
    return path


def test_migration_moves_the_home_tenant_indexes_under_the_tenant(tmp_path, monkeypatch):
    _bundle(tmp_path, "adp", "repositories:\n  svc:\n    index: true\n")
    _bundle(tmp_path, "beta", "repositories:\n  other:\n    index: true\n")
    settings, registries = _registries(tmp_path, monkeypatch)
    roots = IndexRoots(tmp_path / "scip", tmp_path / "zoekt")
    _index(roots.scip, "svc@aaaaaaa")
    _index(roots.zoekt, "svc@aaaaaaa")
    _index(roots.scip, "other@bbbbbbb")

    moved = migrate_home_indexes(registries, settings, roots)

    assert sorted(moved) == sorted(
        [roots.scip / "adp" / "svc@aaaaaaa", roots.zoekt / "adp" / "svc@aaaaaaa"]
    )
    assert (roots.scip / "adp" / "svc@aaaaaaa" / "marker").read_text() == "svc@aaaaaaa"
    assert not (roots.scip / "svc@aaaaaaa").exists()
    assert (roots.scip / "other@bbbbbbb").is_dir()
    assert migrate_home_indexes(registries, settings, roots) == []


def test_migration_keeps_an_index_whose_target_exists(tmp_path, monkeypatch):
    _bundle(tmp_path, "adp", "repositories:\n  svc:\n    index: true\n")
    _bundle(tmp_path, "beta", None)
    settings, registries = _registries(tmp_path, monkeypatch)
    roots = IndexRoots(tmp_path / "scip", tmp_path / "zoekt")
    _index(roots.scip, "svc@aaaaaaa")
    _index(roots.scip / "adp", "svc@aaaaaaa")

    assert migrate_home_indexes(registries, settings, roots) == []
    assert (roots.scip / "svc@aaaaaaa").is_dir()


def test_migration_does_nothing_in_platform_mode(tmp_path, monkeypatch):
    settings, registries = _registries(tmp_path, monkeypatch, tenants_value="", tenant="")
    roots = IndexRoots(tmp_path / "scip", tmp_path / "zoekt")
    _index(roots.scip, "platform-svc@aaaaaaa")

    assert migrate_home_indexes(registries, settings, roots) == []
    assert (roots.scip / "platform-svc@aaaaaaa").is_dir()


@pytest.mark.parametrize("tenant", ["adp", "beta"])
def test_tenant_index_roots_nest_under_the_tenant(tenant):
    base = IndexRoots(Path("/idx/scip"), Path("/idx/zoekt"))

    roots = tenants.tenant_index_roots(tenant, base)

    assert roots == IndexRoots(Path(f"/idx/scip/{tenant}"), Path(f"/idx/zoekt/{tenant}"))
    assert tenants.tenant_index_roots(PLATFORM_TENANT, base) is None
