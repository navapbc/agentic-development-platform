"""Real GHES backend: git mechanics via subprocess, pull requests via the GHES REST API."""

from __future__ import annotations

import asyncio
import base64
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    PRComment,
    PRFeedback,
    PullRequest,
)
from scripts.config import Settings, get_settings


class RealGitHost:
    def __init__(
        self,
        *,
        api_url: str,
        owner: str,
        token: str,
        git_base_url: str = "",
        remote_url_for: Callable[[str], str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._owner = owner
        self._token = token
        self._git_base_url = git_base_url.rstrip("/")
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
        resolved = settings or get_settings()
        return cls(
            api_url=resolved.github_api_url,
            owner=resolved.github_owner,
            token=resolved.github_token,
            git_base_url=resolved.github_git_base_url,
            remote_url_for=remote_url_for,
            transport=transport,
        )

    def _remote_url(self, repo: str) -> str:
        if self._remote_url_for is not None:
            return self._remote_url_for(repo)
        base = self._git_base_url
        if self._token and base.startswith("https://"):
            base = f"https://x-access-token:{self._token}@{base[len('https://') :]}"
        return f"{base}/{self._owner}/{repo}.git"

    async def _git(self, *args: str, cwd: str | None = None) -> str:
        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )

        proc = await asyncio.to_thread(run)
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

    def _client(self) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
        }
        return httpx.AsyncClient(
            base_url=self._api_url,
            headers=headers,
            transport=self._transport,
            timeout=self._timeout,
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._client() as client:
            resp = await client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        async with self._client() as client:
            resp = await client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()

    async def _patch(self, path: str, body: dict[str, Any]) -> Any:
        async with self._client() as client:
            resp = await client.patch(path, json=body)
            resp.raise_for_status()
            return resp.json()

    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        data = await self._get(f"/repos/{self._owner}/{repo}/contents/{path}", params={"ref": ref})
        text = base64.b64decode(data["content"]).decode("utf-8")
        return FileContent(path=path, text=text, ref=ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        data = await self._get("/search/code", params={"q": f"{query} repo:{self._owner}/{repo}"})
        return [CodeHit(path=item["path"], line=0, text="") for item in data.get("items", [])]

    async def open_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        found = await self._get(
            f"/repos/{self._owner}/{repo}/pulls",
            params={"head": f"{self._owner}:{head}", "state": "open"},
        )
        if found:
            existing = found[0]
            updated = await self._patch(
                f"/repos/{self._owner}/{repo}/pulls/{existing['number']}",
                {"title": title, "body": body},
            )
            return _pull_request(repo, updated, created=False)
        created = await self._post(
            f"/repos/{self._owner}/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return _pull_request(repo, created, created=True)

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        pull = await self._get(f"/repos/{self._owner}/{repo}/pulls/{number}")
        raw = await self._get(f"/repos/{self._owner}/{repo}/issues/{number}/comments")
        comments = tuple(PRComment(author=c["user"]["login"], body=c["body"]) for c in raw)
        return PRFeedback(repo=repo, number=number, state=pull["state"], comments=comments)


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
