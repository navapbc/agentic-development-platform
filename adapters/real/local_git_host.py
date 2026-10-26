"""Local git-folder backend: commits onto a local branch with no push and no remote pull request."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    PRFeedback,
    PublishResult,
    PullRequest,
    author_env,
)
from scripts.config import Settings, get_settings
from services import process

_PULLS_FILE = "pulls.json"
_SUMMARY_FILE = "SUMMARY.md"
_PATCH_FILE = "change.patch"


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


class LocalGitHost:
    """A GitHost that operates on a local git working tree without a remote.

    Commits land on a local branch. No push happens and no remote pull request
    is opened. A publish writes a summary and a patch artifact and records a
    local branch state that discovery and promotion read back.
    """

    def __init__(self, root: Path, *, artifacts_root: Path | None = None) -> None:
        self._root = root
        self._artifacts_root = artifacts_root or (root / ".agent-publish")

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LocalGitHost:
        resolved = settings or get_settings()
        root = resolved.source_control_local_root.strip()
        if not root:
            raise ValueError(
                "source_control_local_root is required when source_control_backend is local"
            )
        artifacts = resolved.source_control_local_artifacts_root.strip()
        return cls(Path(root), artifacts_root=Path(artifacts) if artifacts else None)

    def _repo_path(self, repo: str) -> Path:
        return self._root / repo

    def source_location(self, repo: str) -> str:
        return str(self._repo_path(repo))

    def _artifacts_path(self, repo: str) -> Path:
        return self._artifacts_root / repo

    async def _run(
        self,
        *args: str,
        cwd: str | Path,
        allowed: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
    ) -> _GitResult:
        def run() -> process.CompletedProcess[str]:
            return process.run(
                ["git", "-c", "safe.directory=*", *args],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env={**os.environ, **env} if env else None,
            )

        proc = await asyncio.to_thread(run)
        if proc.returncode not in allowed:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"git {' '.join(args)} failed (rc {proc.returncode}): {detail}")
        return _GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())

    async def _git(self, *args: str, cwd: str | Path, env: dict[str, str] | None = None) -> str:
        return (await self._run(*args, cwd=cwd, env=env)).stdout

    async def _branch_exists(self, repo_path: Path, name: str) -> str:
        result = await self._run(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=repo_path, allowed=(0, 1)
        )
        return result.stdout if result.returncode == 0 else ""

    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        text = await self._git("show", f"{ref}:{path}", cwd=self._repo_path(repo))
        return FileContent(path=path, text=text, ref=ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        args = ["grep", "-n", "-e", query]
        if ref:
            args.append(ref)
        result = await self._run(*args, cwd=self._repo_path(repo), allowed=(0, 1))
        hits: list[CodeHit] = []
        for line in result.stdout.splitlines():
            body = line[len(ref) + 1 :] if ref and line.startswith(f"{ref}:") else line
            first, _, rest = body.partition(":")
            number, _, snippet = rest.partition(":")
            if number.isdigit():
                hits.append(CodeHit(path=first, line=int(number), text=snippet))
        return hits

    async def ensure_branch(self, repo: str, name: str, base: str) -> BranchRef:
        repo_path = self._repo_path(repo)
        existing = await self._branch_exists(repo_path, name)
        if existing:
            return BranchRef(repo=repo, name=name, head_sha=existing, created=False)
        await self._git("branch", name, base, cwd=repo_path)
        head = await self._git("rev-parse", name, cwd=repo_path)
        return BranchRef(repo=repo, name=name, head_sha=head, created=True)

    async def commit_and_push(self, request: CommitRequest) -> CommitResult:
        repo_path = self._repo_path(request.repo)
        work = request.workspace
        if not await self._branch_exists(repo_path, request.branch):
            await self._git("branch", request.branch, request.base, cwd=repo_path)
        head = await self._git("rev-parse", request.branch, cwd=repo_path)
        await self._git("fetch", "--quiet", str(repo_path), request.branch, cwd=work)
        if await self._git("rev-parse", f"{head}^{{tree}}", cwd=work) == request.tree:
            return CommitResult(
                repo=request.repo,
                branch=request.branch,
                commit_sha=head,
                pushed=False,
                replayed=True,
                published_tree=request.tree,
            )
        sha = await self._git(
            "commit-tree",
            request.tree,
            "-p",
            head,
            "-m",
            request.message,
            cwd=work,
            env=author_env(request),
        )
        await self._git("push", str(repo_path), f"{sha}:refs/heads/{request.branch}", cwd=work)
        await self._git("reset", "--soft", sha, cwd=work)
        return CommitResult(
            repo=request.repo,
            branch=request.branch,
            commit_sha=sha,
            pushed=False,
            replayed=False,
            published_tree=await self._git(
                "rev-parse", f"{request.branch}^{{tree}}", cwd=repo_path
            ),
        )

    async def open_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        repo_path = self._repo_path(repo)
        artifacts = self._artifacts_path(repo) / head
        artifacts.mkdir(parents=True, exist_ok=True)
        patch = await self._git("diff", f"{base}...{head}", cwd=repo_path)
        (artifacts / _SUMMARY_FILE).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        (artifacts / _PATCH_FILE).write_text(f"{patch}\n" if patch else "", encoding="utf-8")
        pulls = self._load_pulls(repo)
        record = pulls.get(head)
        if record is None:
            number = max((int(r["number"]) for r in pulls.values()), default=0) + 1
            record = {"number": number, "base": base, "created": True}
        else:
            record = {**record, "base": base, "created": False}
        record.update(
            {
                "title": title,
                "body": body,
                "draft": True,
                "state": "open",
                "url": str((artifacts / _SUMMARY_FILE).resolve()),
            }
        )
        pulls[head] = record
        self._save_pulls(repo, pulls)
        return self._pull_request(repo, head, record, created=bool(record.get("created")))

    async def find_open_pr(self, repo: str, head: str) -> PullRequest | None:
        record = self._load_pulls(repo).get(head)
        if record is None or record.get("state") == "closed":
            return None
        return self._pull_request(repo, head, record, created=False)

    async def publish(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PublishResult:
        pr = await self.open_draft_pr(repo, head, base, title, body)
        artifacts = self._artifacts_path(repo) / head
        return PublishResult(
            repo=repo,
            branch=head,
            completion_signal_id=str(pr.number),
            summary_path=str((artifacts / _SUMMARY_FILE).resolve()),
            patch_path=str((artifacts / _PATCH_FILE).resolve()),
            created=pr.created,
        )

    async def find_published_branch(self, repo: str, head: str) -> PublishResult | None:
        record = self._load_pulls(repo).get(head)
        if record is None or record.get("state") == "closed":
            return None
        artifacts = self._artifacts_path(repo) / head
        return PublishResult(
            repo=repo,
            branch=head,
            completion_signal_id=str(record["number"]),
            summary_path=str((artifacts / _SUMMARY_FILE).resolve()),
            patch_path=str((artifacts / _PATCH_FILE).resolve()),
            reused=True,
        )

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        branch, record = self._find_by_number(repo, number)
        state = record.get("state", "open") if record else "closed"
        return PRFeedback(repo=repo, number=number, state=state, comments=())

    async def delete_branch(self, repo: str, name: str) -> bool:
        repo_path = self._repo_path(repo)
        if not await self._branch_exists(repo_path, name):
            return False
        await self._git("branch", "-D", name, cwd=repo_path)
        return True

    async def close_pr(self, repo: str, number: int) -> PullRequest:
        return self._set_record(repo, number, {"state": "closed"})

    async def mark_pr_ready(self, repo: str, number: int) -> PullRequest:
        return self._set_record(repo, number, {"draft": False})

    def _pulls_path(self, repo: str) -> Path:
        return self._artifacts_path(repo) / _PULLS_FILE

    def _load_pulls(self, repo: str) -> dict[str, dict[str, Any]]:
        path = self._pulls_path(repo)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_pulls(self, repo: str, pulls: dict[str, dict[str, Any]]) -> None:
        path = self._pulls_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pulls, indent=2, sort_keys=True), encoding="utf-8")

    def _find_by_number(
        self, repo: str, number: int
    ) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        for branch, record in self._load_pulls(repo).items():
            if int(record["number"]) == number:
                return branch, record
        return None, None

    def _set_record(self, repo: str, number: int, changes: dict[str, Any]) -> PullRequest:
        pulls = self._load_pulls(repo)
        for branch, record in pulls.items():
            if int(record["number"]) == number:
                record.update(changes)
                pulls[branch] = record
                self._save_pulls(repo, pulls)
                return self._pull_request(repo, branch, record, created=False)
        raise KeyError(f"no local pull request #{number} for {repo}")

    def _pull_request(
        self, repo: str, branch: str, record: dict[str, Any], *, created: bool
    ) -> PullRequest:
        return PullRequest(
            repo=repo,
            number=int(record["number"]),
            url=str(record.get("url", "")),
            head=branch,
            base=str(record.get("base", "")),
            title=str(record.get("title", "")),
            body=str(record.get("body", "")),
            draft=bool(record.get("draft", True)),
            state=str(record.get("state", "open")),
            created=created,
        )
