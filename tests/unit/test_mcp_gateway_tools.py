"""Unit tests for the tenant scoping of the MCP gateway's toolset."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.config import Settings
from services.integration.credentials import ConfigError
from services.integration.mcp_gateway import tools
from services.integration.mcp_gateway.evidence_reader import PostgresEvidenceReader
from services.integration.mcp_gateway.tools import ReadOnlyToolset
from services.integration.tenant_config import pinned
from tests.support import hermetic_settings


def _toolset() -> tuple[ReadOnlyToolset, list[tuple[str, str, str]]]:
    calls: list[tuple[str, str, str]] = []

    async def context_fetch(repo: str, commit: str, focus: str) -> dict:
        calls.append((repo, commit, focus))
        return {"repository": repo, "focus": focus}

    toolset = ReadOnlyToolset(
        jira=None,  # pyright: ignore[reportArgumentType]
        github=None,  # pyright: ignore[reportArgumentType]
        evidence=None,  # pyright: ignore[reportArgumentType]
        context_fetch=context_fetch,
        tenant="beta",
    )
    return toolset, calls


async def test_context_package_is_served_for_the_gateway_tenant():
    toolset, calls = _toolset()

    package = await toolset.repo_get_context_package("web", "health", "abc1234")

    assert package == {"repository": "web", "focus": "health"}
    assert calls == [("web", "abc1234", "health")]


class _Recorder:
    def __init__(self) -> None:
        self.settings: list[Settings] = []

    def build(self, settings: Settings) -> object:
        self.settings.append(settings)
        return object()


def _patch_builders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    from services.coordination import idempotency, sequencing
    from services.integration.github import factory
    from services.integration.jira import adapter

    recorders = {"jira": _Recorder(), "git_host": _Recorder()}
    monkeypatch.setattr(adapter, "ProjectRoutingJiraReader", recorders["jira"].build)
    monkeypatch.setattr(factory, "build_git_host", recorders["git_host"].build)
    monkeypatch.setattr(idempotency, "PostgresSideEffectLedger", object)
    monkeypatch.setattr(sequencing, "PostgresSequenceGate", object)
    monkeypatch.setattr(tools, "acceptance_field", lambda settings: "customfield_accept")
    return recorders


def _bundle(directory: Path, repositories: list[str]) -> Path:
    directory.mkdir(parents=True)
    entries = "".join(
        f"  {name}:\n    backend: local\n    path: {directory / name}\n" for name in repositories
    )
    (directory / "repositories.yaml").write_text(
        f"config_version: 1\nrepositories:\n{entries}", encoding="utf-8"
    )
    return directory


def test_the_toolset_reads_with_the_settings_of_its_tenant_only(monkeypatch, tmp_path):
    recorders = _patch_builders(monkeypatch)
    settings = hermetic_settings(tenant="beta")
    bundle = _bundle(tmp_path / "beta", ["beta-web"])

    with pinned(bundle):
        toolset = ReadOnlyToolset.from_settings(settings)

    assert toolset.tenant == "beta"
    assert recorders["jira"].settings[0] is settings
    assert recorders["git_host"].settings[0] is settings
    assert isinstance(toolset.evidence, PostgresEvidenceReader)
    assert toolset.evidence._tenant == "beta"
    assert toolset.github.allowed_repos == frozenset({"beta-web"})  # pyright: ignore[reportAttributeAccessIssue]


async def test_a_repository_outside_the_tenant_bundle_is_refused(monkeypatch, tmp_path):
    from services.integration.github.mediator import RepoNotAllowedError

    _patch_builders(monkeypatch)
    settings = hermetic_settings(tenant="beta")
    bundle = _bundle(tmp_path / "beta", ["beta-web"])

    with pinned(bundle):
        toolset = ReadOnlyToolset.from_settings(settings)

    with pytest.raises(RepoNotAllowedError):
        await toolset.github_read_file("adp-web", "README.md", "main")


def test_the_toolset_refuses_a_gateway_without_a_tenant(monkeypatch):
    _patch_builders(monkeypatch)

    with pytest.raises(ConfigError, match="TENANT is not set"):
        ReadOnlyToolset.from_settings(hermetic_settings(tenant=""))


async def test_the_context_package_carries_the_tenant_repository_intelligence_token(
    monkeypatch,
):
    from services.orchestration import repo_intelligence_client

    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"package": {}})

    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    settings = hermetic_settings(
        tenant="beta",
        repo_intelligence_token="beta-intelligence-credential",
        repo_intelligence_url="http://repository-intelligence.test",
    )
    monkeypatch.setattr(repo_intelligence_client, "get_settings", lambda: settings)
    monkeypatch.setattr(repo_intelligence_client.httpx, "AsyncClient", client)
    toolset = ReadOnlyToolset(
        jira=None,  # pyright: ignore[reportArgumentType]
        github=None,  # pyright: ignore[reportArgumentType]
        evidence=None,  # pyright: ignore[reportArgumentType]
        context_fetch=repo_intelligence_client.fetch_context_package,
        tenant="beta",
    )

    assert await toolset.repo_get_context_package("beta-web", "health") == {"package": {}}
    assert seen[0].headers["Authorization"] == "Bearer beta-intelligence-credential"
