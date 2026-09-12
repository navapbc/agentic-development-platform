"""Real GHES backend: git mechanics via subprocess, pull requests via the GHES REST API."""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    PRComment,
    PRFeedback,
    PublishResult,
    PullRequest,
    publish_from_pull_request,
    reuse_from_pull_request,
)
from scripts.config import Settings, get_settings

if TYPE_CHECKING:
    from services.integration.github.credentials import (
        GitHubCredentialResolver,
        GitHubRepoCredential,
    )

_AUTH_URL = re.compile(r"x-access-token:[^@]*@")


class RealGitHost:
    def __init__(
        self,
        resolver: GitHubCredentialResolver,
        *,
        remote_url_for: Callable[[str], str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._resolver = resolver
        self._remote_url_for = remote_url_for
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        remote_url_for: Callable[[str], str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> RealGitHost:
        from services.integration.github.credentials import build_github_credential_resolver

        resolved = settings or get_settings()
        return cls(
            build_github_credential_resolver(resolved),
            remote_url_for=remote_url_for,
            transport=transport,
        )

    def _credential(self, repo: str) -> GitHubRepoCredential:
        cred = self._resolver.resolve(repo)
        if cred is None:
            raise KeyError(f"repository not resolvable: {repo}")
        return cred

    def _remote_url(self, repo: str) -> str:
        if self._remote_url_for is not None:
            return self._remote_url_for(repo)
        return self._resolver.git_remote_url(repo)

    def source_location(self, repo: str) -> str:
        return self._remote_url(repo)

    async def _git(self, *args: str, cwd: str | None = None) -> str:
        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
            )

        proc = await asyncio.to_thread(run)
        if proc.returncode != 0:
            shown = _AUTH_URL.sub("x-access-token:***@", " ".join(args))
            detail = _AUTH_URL.sub("x-access-token:***@", (proc.stderr or proc.stdout).strip())
            raise RuntimeError(f"git {shown} failed (rc {proc.returncode}): {detail}")
        return proc.stdout.strip()

    async def _ls_remote(self, remote: str, ref: str) -> str:
        out = await self._git("ls-remote", "--heads", remote, ref)
        if not out:
            return ""
        return out.split()[0]

    async def ensure_branch(self, repo: str, name: str, base: str) -> BranchRef:
        remote = self._remote_url(repo)
        existing = await self._ls_remote(remote, name)
        if existing:
            return BranchRef(repo=repo, name=name, head_sha=existing, created=False)
        base_sha = await self._ls_remote(remote, base)
        if not base_sha:
            raise KeyError(f"base branch missing: {repo}:{base}")
        with tempfile.TemporaryDirectory() as work:
            await self._git("clone", "--branch", base, "--single-branch", remote, work)
            await self._git("push", "origin", f"HEAD:refs/heads/{name}", cwd=work)
            head = await self._git("rev-parse", "HEAD", cwd=work)
        return BranchRef(repo=repo, name=name, head_sha=head, created=True)

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        remote = self._remote_url(request.repo)
        trailer = f"execution_id: {request.execution_id}"
        with tempfile.TemporaryDirectory() as work:
            await self._git("clone", remote, work)
            await self._git("config", "user.name", request.author_name, cwd=work)
            await self._git("config", "user.email", request.author_email, cwd=work)
            if await self._ls_remote(remote, request.branch):
                await self._git("checkout", request.branch, cwd=work)
            else:
                await self._git(
                    "checkout", "-b", request.branch, f"origin/{request.base}", cwd=work
                )
            existing = await self._git(
                "log", f"--grep={trailer}", "-n", "1", "--format=%H", cwd=work
            )
            if existing:
                return CommitResult(
                    repo=request.repo,
                    branch=request.branch,
                    commit_sha=existing,
                    pushed=False,
                    replayed=True,
                )
            for path, text in request.files.items():
                target = Path(work) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            await self._git("add", "-A", cwd=work)
            await self._git("commit", "-m", f"{request.message}\n\n{trailer}", cwd=work)
            sha = await self._git("rev-parse", "HEAD", cwd=work)
            await self._git("push", "origin", request.branch, cwd=work)
        return CommitResult(
            repo=request.repo,
            branch=request.branch,
            commit_sha=sha,
            pushed=True,
            replayed=False,
        )

    def _client(self, cred: GitHubRepoCredential) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {cred.token}",
        }
        return httpx.AsyncClient(
            base_url=cred.api_url.rstrip("/"),
            headers=headers,
            transport=self._transport,
            timeout=self._timeout,
        )

    async def _get(
        self, cred: GitHubRepoCredential, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        async with self._client(cred) as client:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, cred: GitHubRepoCredential, path: str, body: dict[str, Any]) -> Any:
        async with self._client(cred) as client:
            resp = await client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()

    async def _patch(self, cred: GitHubRepoCredential, path: str, body: dict[str, Any]) -> Any:
        async with self._client(cred) as client:
            resp = await client.patch(path, json=body)
            resp.raise_for_status()
            return resp.json()

    async def _graphql(
        self, cred: GitHubRepoCredential, query: str, variables: dict[str, Any]
    ) -> Any:
        async with self._client(cred) as client:
            resp = await client.post(
                _graphql_url_from(cred.api_url.rstrip("/")),
                json={"query": query, "variables": variables},
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("errors"):
                raise RuntimeError(f"graphql error: {payload['errors']}")
            return payload.get("data")

    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        cred = self._credential(repo)
        data = await self._get(
            cred, f"/repos/{cred.owner}/{repo}/contents/{path}", params={"ref": ref}
        )
        text = base64.b64decode(data["content"]).decode("utf-8")
        return FileContent(path=path, text=text, ref=ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        cred = self._credential(repo)
        data = await self._get(
            cred, "/search/code", params={"q": f"{query} repo:{cred.owner}/{repo}"}
        )
        return [CodeHit(path=item["path"], line=0, text="") for item in data.get("items", [])]

    async def open_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        cred = self._credential(repo)
        found = await self._get(
            cred,
            f"/repos/{cred.owner}/{repo}/pulls",
            params={"head": f"{cred.owner}:{head}", "state": "open"},
        )
        if found:
            existing = found[0]
            updated = await self._patch(
                cred,
                f"/repos/{cred.owner}/{repo}/pulls/{existing['number']}",
                {"title": title, "body": body},
            )
            return _pull_request(repo, updated, created=False)
        created = await self._post(
            cred,
            f"/repos/{cred.owner}/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return _pull_request(repo, created, created=True)

    async def find_open_pr(self, repo: str, head: str) -> PullRequest | None:
        cred = self._credential(repo)
        found = await self._get(
            cred,
            f"/repos/{cred.owner}/{repo}/pulls",
            params={"head": f"{cred.owner}:{head}", "state": "open"},
        )
        if not found:
            return None
        return _pull_request(repo, found[0], created=False)

    async def publish(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PublishResult:
        pr = await self.open_draft_pr(repo, head, base, title, body)
        return publish_from_pull_request(repo, head, pr)

    async def find_published_branch(self, repo: str, head: str) -> PublishResult | None:
        return reuse_from_pull_request(repo, head, await self.find_open_pr(repo, head))

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        cred = self._credential(repo)
        pull = await self._get(cred, f"/repos/{cred.owner}/{repo}/pulls/{number}")
        raw = await self._get(cred, f"/repos/{cred.owner}/{repo}/issues/{number}/comments")
        comments = tuple(PRComment(author=c["user"]["login"], body=c["body"]) for c in raw)
        return PRFeedback(repo=repo, number=number, state=pull["state"], comments=comments)

    async def delete_branch(self, repo: str, name: str) -> bool:
        remote = self._remote_url(repo)
        if not await self._ls_remote(remote, name):
            return False
        await self._git("push", remote, "--delete", f"refs/heads/{name}")
        return True

    async def close_pr(self, repo: str, number: int) -> PullRequest:
        cred = self._credential(repo)
        updated = await self._patch(
            cred, f"/repos/{cred.owner}/{repo}/pulls/{number}", {"state": "closed"}
        )
        return _pull_request(repo, updated, created=False)

    async def mark_pr_ready(self, repo: str, number: int) -> PullRequest:
        cred = self._credential(repo)
        pull = await self._get(cred, f"/repos/{cred.owner}/{repo}/pulls/{number}")
        if not bool(pull.get("draft", False)):
            return _pull_request(repo, pull, created=False)
        mutation = (
            "mutation MarkReady($id: ID!) { "
            "markPullRequestReadyForReview(input: {pullRequestId: $id}) "
            "{ pullRequest { number } } }"
        )
        await self._graphql(cred, mutation, {"id": pull.get("node_id", "")})
        refreshed = await self._get(cred, f"/repos/{cred.owner}/{repo}/pulls/{number}")
        return _pull_request(repo, refreshed, created=False)


def _graphql_url_from(api_url: str) -> str:
    suffix = "/api/v3"
    if api_url.endswith(suffix):
        return f"{api_url[: -len(suffix)]}/api/graphql"
    return f"{api_url}/graphql"


def _pull_request(repo: str, data: dict[str, Any], *, created: bool) -> PullRequest:
    head = data.get("head", {})
    base = data.get("base", {})
    return PullRequest(
        repo=repo,
        number=int(data["number"]),
        url=data.get("html_url", ""),
        head=head.get("ref", "") if isinstance(head, dict) else str(head),
        base=base.get("ref", "") if isinstance(base, dict) else str(base),
        title=data.get("title", ""),
        body=data.get("body") or "",
        draft=bool(data.get("draft", True)),
        state=data.get("state", "open"),
        created=created,
    )
