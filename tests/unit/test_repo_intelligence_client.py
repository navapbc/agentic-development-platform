"""Unit tests for the repository-intelligence HTTP client."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from services.integration.credentials import ConfigError
from services.orchestration import repo_intelligence_client
from services.repository_intelligence.tenant_tokens import derive_token
from tests.support import hermetic_settings


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    settings = hermetic_settings(repo_intelligence_token="tenant-token")
    monkeypatch.setattr(repo_intelligence_client, "get_settings", lambda: settings)


def test_fetch_context_package_posts_payload(monkeypatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"focus": "health", "repository": "agentic-demo-service"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(repo_intelligence_client.httpx, "AsyncClient", factory)

    result = asyncio.run(
        repo_intelligence_client.fetch_context_package("agentic-demo-service", "0744a6e", "health")
    )

    assert seen["path"] == "/context_package"
    assert seen["body"] == {
        "repo": "agentic-demo-service",
        "commit": "0744a6e",
        "focus": "health",
        "detail": "",
    }
    assert seen["authorization"] == "Bearer tenant-token"
    assert result["focus"] == "health"


def test_fetch_context_package_raises_on_error(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "x"}))
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(repo_intelligence_client.httpx, "AsyncClient", factory)

    try:
        asyncio.run(repo_intelligence_client.fetch_context_package("r", "c", "f"))
    except httpx.HTTPStatusError:
        return
    raise AssertionError("expected HTTPStatusError")


def test_the_token_is_derived_from_the_key_and_tenant_without_an_explicit_token():
    settings = hermetic_settings(repo_intelligence_token_key="key", tenant="beta")

    assert repo_intelligence_client.repo_intelligence_headers(settings) == {
        "Authorization": f"Bearer {derive_token('key', 'beta')}"
    }


def test_the_explicit_token_wins_over_the_key():
    settings = hermetic_settings(
        repo_intelligence_token="explicit", repo_intelligence_token_key="key", tenant="beta"
    )

    assert repo_intelligence_client.repo_intelligence_token(settings) == "explicit"


@pytest.mark.parametrize(
    "overrides",
    [{}, {"repo_intelligence_token_key": "key"}, {"tenant": "beta"}],
)
def test_a_missing_token_raises_an_error_naming_the_variable(overrides):
    with pytest.raises(ConfigError, match="REPO_INTELLIGENCE_TOKEN"):
        repo_intelligence_client.repo_intelligence_token(hermetic_settings(**overrides))


def test_fetch_context_package_without_a_token_sends_no_request(monkeypatch):
    monkeypatch.setattr(repo_intelligence_client, "get_settings", lambda: hermetic_settings())

    def factory(*args, **kwargs):
        raise AssertionError("no request is sent without a token")

    monkeypatch.setattr(repo_intelligence_client.httpx, "AsyncClient", factory)

    with pytest.raises(ConfigError, match="REPO_INTELLIGENCE_TOKEN"):
        asyncio.run(repo_intelligence_client.fetch_context_package("r", "c", "f"))
