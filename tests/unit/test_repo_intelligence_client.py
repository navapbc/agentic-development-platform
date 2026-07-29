"""Unit tests for the repository-intelligence HTTP client."""

from __future__ import annotations

import asyncio
import json

import httpx
from services.orchestration import repo_intelligence_client


def test_fetch_context_package_posts_payload(monkeypatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
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
    }
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
