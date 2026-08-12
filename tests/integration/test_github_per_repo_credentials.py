"""Two allowlisted repos resolve distinct GitHub credentials for REST and git, no sharing."""

from __future__ import annotations

import httpx
from adapters.real.git_host import RealGitHost
from services.integration.credentials import EnvSecretResolver
from services.integration.github.credentials import (
    GitHubCredentialResolver,
    parse_github_config,
)

RAW = {
    "hosts": {
        "acme": {"api_url": "https://acme.ghe/api/v3", "git_base_url": "https://acme.ghe"},
        "globex": {"api_url": "https://globex.ghe/api/v3", "git_base_url": "https://globex.ghe"},
    },
    "default": {"host": "acme", "owner": "acme-org", "token_ref": "ACME_TOKEN"},
    "repositories": {
        "repo-b": {"host": "globex", "owner": "globex-org", "token_ref": "GLOBEX_TOKEN"},
    },
}


def _resolver() -> GitHubCredentialResolver:
    return GitHubCredentialResolver(
        parse_github_config(RAW),
        {"repo-a", "repo-b"},
        EnvSecretResolver({"ACME_TOKEN": "tok-a", "GLOBEX_TOKEN": "tok-b"}),
    )


def _transport(calls: list[tuple[str, str | None]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("authorization")))
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={
                    "number": 7,
                    "html_url": "https://x/pr/7",
                    "head": {"ref": "agent/x"},
                    "base": {"ref": "main"},
                    "title": "t",
                    "body": "b",
                    "draft": True,
                    "state": "open",
                },
            )
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


async def test_rest_calls_use_each_repository_own_owner_and_token() -> None:
    calls: list[tuple[str, str | None]] = []
    host = RealGitHost(_resolver(), transport=_transport(calls))

    await host.open_draft_pr("repo-a", "agent/x", "main", "t", "b")
    await host.open_draft_pr("repo-b", "agent/x", "main", "t", "b")

    a_calls = [(url, auth) for url, auth in calls if "repo-a" in url]
    b_calls = [(url, auth) for url, auth in calls if "repo-b" in url]

    assert a_calls
    assert all(
        "acme.ghe" in url and "/repos/acme-org/repo-a/" in url and auth == "Bearer tok-a"
        for url, auth in a_calls
    )
    assert b_calls
    assert all(
        "globex.ghe" in url and "/repos/globex-org/repo-b/" in url and auth == "Bearer tok-b"
        for url, auth in b_calls
    )
    assert not any("tok-b" in (auth or "") for _, auth in a_calls)
    assert not any("tok-a" in (auth or "") for _, auth in b_calls)


def test_git_remote_url_carries_each_repository_own_token_and_owner() -> None:
    resolver = _resolver()

    assert (
        resolver.git_remote_url("repo-a")
        == "https://x-access-token:tok-a@acme.ghe/acme-org/repo-a.git"
    )
    assert (
        resolver.git_remote_url("repo-b")
        == "https://x-access-token:tok-b@globex.ghe/globex-org/repo-b.git"
    )


def test_non_allowlisted_repository_resolves_nothing() -> None:
    assert _resolver().resolve("repo-c") is None
