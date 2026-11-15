"""Unit tests for the MCP gateway's repository context tool."""

from __future__ import annotations

import pytest
from services.integration.mcp_gateway.tools import ContextPackageRefusedError, ReadOnlyToolset


def _toolset(tenants: tuple[str, ...]) -> tuple[ReadOnlyToolset, list[tuple[str, str, str]]]:
    calls: list[tuple[str, str, str]] = []

    async def context_fetch(repo: str, commit: str, focus: str) -> dict:
        calls.append((repo, commit, focus))
        return {"repository": repo, "focus": focus}

    toolset = ReadOnlyToolset(
        jira=None,  # pyright: ignore[reportArgumentType]
        github=None,  # pyright: ignore[reportArgumentType]
        evidence=None,  # pyright: ignore[reportArgumentType]
        context_fetch=context_fetch,
        tenants=tenants,
    )
    return toolset, calls


async def test_context_package_is_refused_when_tenants_names_more_than_one_tenant():
    toolset, calls = _toolset(("adp", "beta"))

    with pytest.raises(ContextPackageRefusedError, match="TENANTS names 2 tenants"):
        await toolset.repo_get_context_package("web", "health")

    assert calls == []


@pytest.mark.parametrize("tenants", [(), ("adp",)])
async def test_context_package_uses_the_client_with_at_most_one_tenant(tenants):
    toolset, calls = _toolset(tenants)

    package = await toolset.repo_get_context_package("web", "health", "abc1234")

    assert package == {"repository": "web", "focus": "health"}
    assert calls == [("web", "abc1234", "health")]
