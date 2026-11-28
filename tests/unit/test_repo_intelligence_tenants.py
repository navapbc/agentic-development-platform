"""Unit tests for the per-tenant repository registries of repository intelligence."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from services.integration.credentials import ConfigError
from services.integration.tenant_config import DirectorySource
from services.repository_intelligence import tenants
from services.repository_intelligence.indexing.reindex import IndexRoots
from services.repository_intelligence.tenant_tokens import derive_token
from services.repository_intelligence.tenants import (
    TenantRegistries,
    configured_tokens,
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


def test_served_tenants_prefers_tenants_then_tenant():
    assert served_tenants(hermetic_settings(tenants="adp, beta", tenant="beta")) == ["adp", "beta"]
    assert served_tenants(hermetic_settings(tenants="", tenant="adp")) == ["adp"]
    assert served_tenants(hermetic_settings(tenants="", tenant="")) == []


def test_each_tenant_reads_only_its_bundle(tmp_path, monkeypatch, caplog):
    _bundle(tmp_path, "beta", "repositories:\n  beta-web:\n    index: false\n")
    _bundle(tmp_path, "adp", None)
    _, registries = _registries(tmp_path, monkeypatch)

    with caplog.at_level(logging.ERROR):
        adp = registries.registry("adp")
    beta = registries.registry("beta")

    assert adp is None
    assert "tenant adp has no repositories.yaml" in caplog.text
    assert beta is not None and beta.names() == {"beta-web"}
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
    _bundle(tmp_path, "adp", "repositories:\n  adp-web: {}\n")
    _, registries = _registries(tmp_path, monkeypatch)

    assert registries.registry("beta") is None
    assert registries.registry("adp") is not None


def test_registries_without_a_tenant_refuse_to_start(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="no tenant is served"):
        _registries(tmp_path, monkeypatch, tenants_value="", tenant="")


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


@pytest.mark.parametrize("tenant", ["adp", "beta"])
def test_tenant_index_roots_nest_under_the_tenant(tenant):
    base = IndexRoots(Path("/idx/scip"), Path("/idx/zoekt"))

    roots = tenants.tenant_index_roots(tenant, base)

    assert roots == IndexRoots(Path(f"/idx/scip/{tenant}"), Path(f"/idx/zoekt/{tenant}"))
