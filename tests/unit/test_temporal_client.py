"""TLS selection for the single Temporal connection helper."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.config import Settings
from services import temporal_client
from temporalio.service import TLSConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _settings(**values: str) -> Settings:
    return Settings(_env_file=None, temporal_namespace="platform", **values)


def test_tls_is_off_without_either_setting() -> None:
    assert temporal_client.tls_config(_settings()) is False


def test_an_inline_ca_bundle_verifies_the_frontend_under_the_server_name() -> None:
    tls = temporal_client.tls_config(
        _settings(temporal_tls_ca=CA_PEM, temporal_tls_server_name="frontend.temporal.internal")
    )

    assert tls == TLSConfig(
        server_root_ca_cert=CA_PEM.strip().encode(), domain="frontend.temporal.internal"
    )


def test_a_ca_path_is_read_from_the_file(tmp_path) -> None:
    bundle = tmp_path / "ca.pem"
    bundle.write_text(CA_PEM, encoding="utf-8")

    tls = temporal_client.tls_config(_settings(temporal_tls_ca=str(bundle)))

    assert tls == TLSConfig(server_root_ca_cert=CA_PEM.encode(), domain=None)


def test_a_server_name_alone_uses_the_system_trust_store() -> None:
    tls = temporal_client.tls_config(_settings(temporal_tls_server_name="temporal.example.com"))

    assert tls == TLSConfig(server_root_ca_cert=None, domain="temporal.example.com")


def test_a_ca_path_that_is_not_a_file_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="TEMPORAL_TLS_CA names"):
        temporal_client.tls_config(_settings(temporal_tls_ca=str(tmp_path / "missing.pem")))


async def test_connect_dials_the_target_in_the_namespace_with_the_tls_choice(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def _connect(target_host: str, **kwargs):
        calls.append((target_host, kwargs))
        return "client"

    monkeypatch.setattr(temporal_client.Client, "connect", _connect)

    client = await temporal_client.connect(
        "temporal-nlb:7233",
        _settings(temporal_tls_server_name="frontend.temporal.internal"),
        interceptors=["tracing"],
    )

    assert client == "client"
    [(target, options)] = calls
    assert target == "temporal-nlb:7233"
    assert options["namespace"] == "platform"
    assert options["tls"] == TLSConfig(domain="frontend.temporal.internal")
    assert options["interceptors"] == ["tracing"]


async def test_connect_keeps_tls_off_locally(monkeypatch) -> None:
    calls: list[dict] = []

    async def _connect(target_host: str, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(temporal_client.Client, "connect", _connect)

    await temporal_client.connect("localhost:7233", _settings())

    assert calls[0]["tls"] is False


def test_no_client_connects_outside_the_helper() -> None:
    helper = REPO_ROOT / "services" / "temporal_client.py"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for root in ("services", "scripts", "adapters", "database")
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
        if path != helper
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\bClient\.connect\(", line)
    ]

    assert offenders == []


SKIPPED_DIRECTORIES = {".git", ".venv", ".mypy_cache", ".ruff_cache", "__pycache__", "node_modules"}


def test_no_client_connects_outside_the_helper_anywhere_in_the_repository() -> None:
    helper = REPO_ROOT / "services" / "temporal_client.py"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in sorted(REPO_ROOT.rglob("*.py"))
        if path != helper and not SKIPPED_DIRECTORIES & set(path.relative_to(REPO_ROOT).parts)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\bClient\.connect\(", line)
    ]

    assert offenders == []
