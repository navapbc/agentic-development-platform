"""Rewriting Compose-internal service addresses to their host-published form."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from scripts.config import (
    _COMPOSE_PUBLISHED_PORTS,
    Settings,
    alembic_database_url,
    get_settings,
    to_host_hostport,
    to_host_url,
)

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "compose.yaml"


@pytest.fixture(autouse=True)
def on_the_host(monkeypatch):
    monkeypatch.delenv("PLATFORM_IN_CONTAINER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_host_url_uses_the_published_port_when_remapped():
    settings = Settings(postgres_host_port=15432)

    rewritten = to_host_url("postgresql://postgres:5432/agent_platform", settings)

    assert rewritten == "postgresql://localhost:15432/agent_platform"


def test_host_url_keeps_the_default_port_when_unmapped():
    settings = Settings()

    rewritten = to_host_url("postgresql://postgres:5432/agent_platform", settings)

    assert rewritten == "postgresql://localhost:5432/agent_platform"


def test_host_url_preserves_credentials_across_the_port_remap():
    settings = Settings(postgres_host_port=15432)

    rewritten = to_host_url("postgresql://platform:secret@postgres:5432/agent_platform", settings)

    assert rewritten == "postgresql://platform:secret@localhost:15432/agent_platform"


def test_host_hostport_uses_the_published_port_when_remapped():
    settings = Settings(temporal_host_port=17233)

    assert to_host_hostport("temporal:7233", settings) == "localhost:17233"


def test_host_url_leaves_a_foreign_hostname_alone():
    settings = Settings(postgres_host_port=15432)

    assert to_host_url("https://api.github.com/x", settings) == "https://api.github.com/x"


def test_host_url_is_inert_inside_the_platform_image(monkeypatch):
    monkeypatch.setenv("PLATFORM_IN_CONTAINER", "1")
    settings = Settings(postgres_host_port=15432)

    url = "postgresql://postgres:5432/agent_platform"

    assert to_host_url(url, settings) == url


def test_otel_collector_maps_each_of_its_three_ports_separately():
    settings = Settings(
        otel_grpc_host_port=14317, otel_http_host_port=14318, otel_metrics_host_port=18889
    )

    assert to_host_url("http://otel-collector:4317", settings) == "http://localhost:14317"
    assert to_host_url("http://otel-collector:4318", settings) == "http://localhost:14318"
    assert to_host_url("http://otel-collector:8889", settings) == "http://localhost:18889"


def test_host_hostport_returns_a_bare_service_name_without_a_port():
    settings = Settings(temporal_host_port=17233)

    assert to_host_hostport("temporal", settings) == "localhost"


def test_host_url_without_a_port_gains_none():
    settings = Settings(langfuse_web_host_port=13000)

    assert to_host_url("http://langfuse-web/api", settings) == "http://localhost/api"


def test_every_rewritable_service_publishes_through_a_variable():
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    defaults = Settings()

    for service, port_map in _COMPOSE_PUBLISHED_PORTS.items():
        published = {
            entry.rsplit(":", 1)[0]: entry.rsplit(":", 1)[1]
            for entry in compose["services"][service]["ports"]
        }
        for internal_port, field in port_map.items():
            variable = field.upper()
            expected = f"${{{variable}:-{getattr(defaults, field)}}}"
            assert published.get(expected) == str(internal_port), (
                f"{service}:{internal_port} must publish through {expected}"
            )


def test_alembic_database_url_follows_the_published_postgres_port(monkeypatch):
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST_PORT", "15432")

    assert ":15432/agent_platform" in alembic_database_url()


def test_alembic_database_url_rewrites_a_configured_service_address(monkeypatch):
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL", "postgresql+psycopg://platform:pw@postgres:5432/agent_platform"
    )
    monkeypatch.setenv("POSTGRES_HOST_PORT", "15432")

    assert (
        alembic_database_url() == "postgresql+psycopg://platform:pw@localhost:15432/agent_platform"
    )


def test_alembic_database_url_derives_the_configured_credentials(monkeypatch):
    secret = "".join(("pg", "-", "fixture", "-", "secret"))
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql+asyncpg://platform:{secret}@postgres:5432/agent_platform"
    )
    monkeypatch.setenv("POSTGRES_HOST_PORT", "15432")

    assert alembic_database_url() == (
        f"postgresql+psycopg://platform:{secret}@localhost:15432/agent_platform"
    )
