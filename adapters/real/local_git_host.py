"""Local git-folder backend: commits onto a local branch with no push and no remote pull request."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
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

RESULTS_REF_PREFIX = "refs/agent-results/"
PULL_FILE = "pull.json"
SUMMARY_FILE = "SUMMARY.md"
PATCH_FILE = "change.patch"
_RESULTS_IDENTITY = {
    "GIT_AUTHOR_NAME": "agentic-dev-platform",
    "GIT_AUTHOR_EMAIL": "agentic-dev-platform@localhost",
    "GIT_COMMITTER_NAME": "agentic-dev-platform",
    "GIT_COMMITTER_EMAIL": "agentic-dev-platform@localhost",
}


def results_ref(branch: str) -> str:
    return f"{RESULTS_REF_PREFIX}{branch}"


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


class LocalGitHost:
    """A GitHost that operates on a local git working tree without a remote.

    Commits land on a local branch. No push happens and no remote pull request
    is opened. A publish commits a summary, a patch, and the local pull record to
    refs/agent-results/<branch> in the same repository, which discovery and promotion
    read back and which travels with the branch when the repository is copied.
    """

    def __init__(self, root: Path, *, path_for: Callable[[str], Path] | None = None) -> None:
        self._root = root
        self._path_for = path_for

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LocalGitHost:
        from services.integration.repositories import load_repository_registry

        resolved = settings or get_settings()
        root = resolved.repository_root.strip()
        if not root:
            raise ValueError("repository_root is required for a repository with backend local")
        registry = load_repository_registry(resolved)
        return cls(Path(root), path_for=lambda repo: Path(registry.location(repo)))

    def _repo_path(self, repo: str) -> Path:
        return self._path_for(repo) if self._path_for is not None else self._root / repo

    def source_location(self, repo: str) -> str:
        return str(self._repo_path(repo))

    async def _run(
        self,
        *args: str,
        cwd: str | Path,
        allowed: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        strip: bool = True,
    ) -> _GitResult:
        def run() -> process.CompletedProcess[str]:
            return process.run(
                ["git", "-c", "safe.directory=*", *args],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                input=stdin,
                env={**os.environ, **env} if env else None,
            )

        proc = await asyncio.to_thread(run)
        if proc.returncode not in allowed:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"git {' '.join(args)} failed (rc {proc.returncode}): {detail}")
        stdout = proc.stdout.strip() if strip else proc.stdout
        return _GitResult(proc.returncode, stdout, proc.stderr.strip())

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
        workspace_head = await self._git("rev-parse", "HEAD", cwd=work)
        parents = ["-p", head]
        if await self._git("merge-base", workspace_head, head, cwd=work) != workspace_head:
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
        patch = await self._git("diff", f"{base}...{head}", cwd=repo_path)
        pulls = await self._load_pulls(repo)
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
                "url": f"{results_ref(head)}:{SUMMARY_FILE}",
            }
        )
        await self._write_results(
            repo,
            head,
            {
                SUMMARY_FILE: f"# {title}\n\n{body}\n",
                PATCH_FILE: f"{patch}\n" if patch else "",
                PULL_FILE: _record_text(record),
            },
        )
        return self._pull_request(repo, head, record, created=bool(record.get("created")))

    async def find_open_pr(self, repo: str, head: str) -> PullRequest | None:
        record = await self._read_record(repo, head)
        if record is None or record.get("state") == "closed":
            return None
        return self._pull_request(repo, head, record, created=False)

    async def publish(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PublishResult:
        pr = await self.open_draft_pr(repo, head, base, title, body)
        return PublishResult(
            repo=repo,
            branch=head,
            completion_signal_id=str(pr.number),
            summary_path=f"{results_ref(head)}:{SUMMARY_FILE}",
            patch_path=f"{results_ref(head)}:{PATCH_FILE}",
            created=pr.created,
        )

    async def find_published_branch(self, repo: str, head: str) -> PublishResult | None:
        record = await self._read_record(repo, head)
        if record is None or record.get("state") == "closed":
            return None
        return PublishResult(
            repo=repo,
            branch=head,
            completion_signal_id=str(record["number"]),
            summary_path=f"{results_ref(head)}:{SUMMARY_FILE}",
            patch_path=f"{results_ref(head)}:{PATCH_FILE}",
            reused=True,
        )

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        _, record = await self._find_by_number(repo, number)
        state = record.get("state", "open") if record else "closed"
        return PRFeedback(repo=repo, number=number, state=state, comments=())

    async def delete_branch(self, repo: str, name: str) -> bool:
        repo_path = self._repo_path(repo)
        if not await self._branch_exists(repo_path, name):
            return False
        await self._git("branch", "-D", name, cwd=repo_path)
        return True

    async def close_pr(self, repo: str, number: int) -> PullRequest:
        return await self._set_record(repo, number, {"state": "closed"})

    async def mark_pr_ready(self, repo: str, number: int) -> PullRequest:
        return await self._set_record(repo, number, {"draft": False})

    async def read_result(self, repo: str, branch: str, name: str) -> str | None:
        """Return one file of the branch's published results, or None when it has none."""
        result = await self._run(
            "show",
            f"{results_ref(branch)}:{name}",
            cwd=self._repo_path(repo),
            allowed=(0, 128),
            strip=False,
        )
        return result.stdout if result.returncode == 0 else None

    async def _read_record(self, repo: str, branch: str) -> dict[str, Any] | None:
        text = await self.read_result(repo, branch, PULL_FILE)
        return json.loads(text) if text else None

    async def _load_pulls(self, repo: str) -> dict[str, dict[str, Any]]:
        listing = await self._git(
            "for-each-ref", "--format=%(refname)", RESULTS_REF_PREFIX, cwd=self._repo_path(repo)
        )
        pulls: dict[str, dict[str, Any]] = {}
        for ref in filter(None, listing.splitlines()):
            branch = ref.removeprefix(RESULTS_REF_PREFIX)
            record = await self._read_record(repo, branch)
            if record is not None:
                pulls[branch] = record
        return pulls

    async def _write_results(self, repo: str, branch: str, files: dict[str, str]) -> None:
        """Commit files onto the branch's results ref, keeping any file the update leaves out."""
        repo_path = self._repo_path(repo)
        ref = results_ref(branch)
        previous = await self._run(
            "rev-parse", "--verify", "--quiet", ref, cwd=repo_path, allowed=(0, 1)
        )
        parent = previous.stdout if previous.returncode == 0 else ""
        contents = dict(files)
        if parent:
            for name in (SUMMARY_FILE, PATCH_FILE, PULL_FILE):
                if name not in contents:
                    kept = await self.read_result(repo, branch, name)
                    if kept is not None:
                        contents[name] = kept
        entries = []
        for name in sorted(contents):
            blob = await self._run(
                "hash-object", "-w", "--stdin", cwd=repo_path, stdin=contents[name]
            )
            entries.append(f"100644 blob {blob.stdout}\t{name}")
        tree = await self._run("mktree", cwd=repo_path, stdin="\n".join(entries) + "\n")
        commit = await self._git(
            "commit-tree",
            tree.stdout,
            *(["-p", parent] if parent else []),
            "-m",
            f"Agent results for {branch}",
            cwd=repo_path,
            env=_RESULTS_IDENTITY,
        )
        await self._git("update-ref", ref, commit, *([parent] if parent else []), cwd=repo_path)

    async def _find_by_number(
        self, repo: str, number: int
    ) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        for branch, record in (await self._load_pulls(repo)).items():
            if int(record["number"]) == number:
                return branch, record
        return None, None

    async def _set_record(self, repo: str, number: int, changes: dict[str, Any]) -> PullRequest:
        branch, record = await self._find_by_number(repo, number)
        if branch is None or record is None:
            raise KeyError(f"no local pull request #{number} for {repo}")
        record.update(changes)
        await self._write_results(repo, branch, {PULL_FILE: _record_text(record)})
        return self._pull_request(repo, branch, record, created=False)

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


def _record_text(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True) + "\n"
