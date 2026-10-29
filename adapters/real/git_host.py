"""Real GHES backend: git mechanics via subprocess, pull requests via the GHES REST API."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
from collections.abc import Callable
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
    author_env,
    publish_from_pull_request,
    reuse_from_pull_request,
)
from scripts.config import Settings, get_settings
from services import process
from services.integration.github.git_auth import NO_AUTH, GitAuth

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

    def _auth(self, repo: str) -> GitAuth:
        cred = self._resolver.resolve(repo)
        return GitAuth(cred.token, cred.git_base_url) if cred is not None else NO_AUTH

    async def _git(
        self,
        *args: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        auth: GitAuth = NO_AUTH,
    ) -> str:
        merged = {**auth.env(), **(env or {})}

        def run() -> process.CompletedProcess[str]:
            return process.run(
                ["git", *auth.config_args(), *args],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                cwd=cwd,
                capture_output=True,
                text=True,
                env={**os.environ, **merged},
            )

        proc = await asyncio.to_thread(run)
        if proc.returncode != 0:
            shown = _AUTH_URL.sub("x-access-token:***@", " ".join(args))
            detail = _AUTH_URL.sub("x-access-token:***@", (proc.stderr or proc.stdout).strip())
            raise RuntimeError(f"git {shown} failed (rc {proc.returncode}): {detail}")
        return proc.stdout.strip()

    async def _ls_remote(self, remote: str, ref: str, auth: GitAuth = NO_AUTH) -> str:
        out = await self._git("ls-remote", "--heads", "--", remote, ref, auth=auth)
        if not out:
            return ""
        return out.split()[0]

    async def ensure_branch(self, repo: str, name: str, base: str) -> BranchRef:
        remote = self._remote_url(repo)
        auth = self._auth(repo)
        existing = await self._ls_remote(remote, name, auth)
        if existing:
            return BranchRef(repo=repo, name=name, head_sha=existing, created=False)
        base_sha = await self._ls_remote(remote, base, auth)
        if not base_sha:
            raise KeyError(f"base branch missing: {repo}:{base}")
        with tempfile.TemporaryDirectory() as work:
            await self._git(
                "clone", "--branch", base, "--single-branch", "--", remote, work, auth=auth
            )
            await self._git("push", "origin", f"HEAD:refs/heads/{name}", cwd=work, auth=auth)
            head = await self._git("rev-parse", "HEAD", cwd=work)
        return BranchRef(repo=repo, name=name, head_sha=head, created=True)

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        """Publish the tree the workspace staged, then read back what the remote now holds.

        The workspace is the checkout the platform ran its checks against, so committing its
        tree object is what keeps the pushed tree equal to the checked one. Rebuilding the commit
        in a separate clone from a mapping of path to text dropped deletions, lost file modes,
        turned renames into an add without the matching delete, and could not carry bytes that
        are not valid UTF-8.

        `commit-tree` rather than `commit` because the branch survives between executions while
        the workspace is reset to the base for each one. Naming the remote head as the parent
        publishes the verified tree on top of whatever the branch already carries, where a plain
        commit would build on the workspace parent and be rejected as a non-fast-forward.
        When the base moved after an earlier execution published, the workspace commit is not in
        the branch history, so it becomes a second parent. Without it the base is not an ancestor
        of the branch and the pull request diff carries every change the base gained since.
        It also runs no hook and reads no `commit.gpgsign`, both of which have produced a
        reported success with no commit behind it when the client repository configured them.

        Idempotency is the tree rather than a per-execution marker, because the repair loop
        publishes several distinct commits within one execution and a per-execution marker
        collapsed them into the first.
        """
        remote = self._remote_url(request.repo)
        auth = self._auth(request.repo)
        work = request.workspace
        remote_head = await self._ls_remote(remote, request.branch, auth)
        if remote_head:
            await self._git("fetch", "--quiet", remote, request.branch, cwd=work, auth=auth)
            if await self._tree_of(work, remote_head) == request.tree:
                return CommitResult(
                    repo=request.repo,
                    branch=request.branch,
                    commit_sha=remote_head,
                    pushed=False,
                    replayed=True,
                    published_tree=request.tree,
                )
        workspace_head = await self._git("rev-parse", "HEAD", cwd=work)
        parents = ["-p", remote_head or workspace_head]
        if remote_head and not await self._is_ancestor(work, workspace_head, remote_head):
            parents += ["-p", workspace_head]
        sha = await self._git(
            "commit-tree",
            request.tree,
            *parents,
            "-m",
            request.message,
            cwd=work,
            env=author_env(request),
        )
        await self._git("push", remote, f"{sha}:refs/heads/{request.branch}", cwd=work, auth=auth)
        await self._git("reset", "--soft", sha, cwd=work)
        published_head = await self._ls_remote(remote, request.branch, auth)
        if not published_head:
            raise RuntimeError(
                f"{request.repo}:{request.branch} holds no ref after pushing {sha}, "
                "so the published tree cannot be verified"
            )
        await self._git("fetch", "--quiet", remote, request.branch, cwd=work, auth=auth)
        return CommitResult(
            repo=request.repo,
            branch=request.branch,
            commit_sha=sha,
            pushed=True,
            replayed=False,
            published_tree=await self._tree_of(work, published_head),
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

    async def _tree_of(self, work: str, commit: str) -> str:
        return await self._git("rev-parse", f"{commit}^{{tree}}", cwd=work)

    async def _is_ancestor(self, work: str, ancestor: str, descendant: str) -> bool:
        return await self._git("merge-base", ancestor, descendant, cwd=work) == ancestor

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
        auth = self._auth(repo)
        if not await self._ls_remote(remote, name, auth):
            return False
        await self._git("push", remote, "--delete", f"refs/heads/{name}", auth=auth)
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
