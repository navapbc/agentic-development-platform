"""Deterministic execution-workspace materialization and branch registration."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from adapters.interfaces.git_host import CommitRequest, agent_branch_name
from scripts.config import Settings, get_settings

from services import process
from services.coordination.orphans import BRANCH, STAGING, OrphanArtifact, OrphanRegistry
from services.integration.github.git_auth import NO_AUTH, GitAuth
from services.integration.github.mediator import RepoNotAllowedError

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _within(path: str, allowed: Sequence[str]) -> bool:
    return any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in allowed)


@dataclass(frozen=True)
class StagedChange:
    tree: str
    changed: bool
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterializedWorkspace:
    repo: str
    task_id: str
    execution_id: str
    branch: str
    base: str
    commit_sha: str
    path: str
    branch_orphan_id: str
    workspace_orphan_id: str


def _slug(value: str) -> str:
    return _SLUG.sub("-", value).strip("-") or "x"


def _run_git(args: list[str], cwd: str | None, auth: GitAuth = NO_AUTH) -> str:
    env = auth.env()
    proc = process.run(
        ["git", "-c", "safe.directory=*", *auth.config_args(), *args],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}): {detail}")
    return proc.stdout.rstrip()


async def _git(*args: str, cwd: str | None = None, auth: GitAuth = NO_AUTH) -> str:
    return await asyncio.to_thread(_run_git, list(args), cwd, auth)


def _run_git_ok(args: list[str], cwd: str | None) -> bool:
    proc = process.run(
        ["git", "-c", "safe.directory=*", *args],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


async def _git_ok(*args: str, cwd: str | None = None) -> bool:
    return await asyncio.to_thread(_run_git_ok, list(args), cwd)


@dataclass
class WorkspaceMaterializer:
    registry: OrphanRegistry
    allowed_repos: frozenset[str]
    workspace_root: Path
    remote_url_for: Callable[[str], str]
    auth_for: Callable[[str], GitAuth] | None = None

    @classmethod
    def from_settings(
        cls,
        registry: OrphanRegistry,
        *,
        remote_url_for: Callable[[str], str] | None = None,
        auth_for: Callable[[str], GitAuth] | None = None,
        settings: Settings | None = None,
    ) -> WorkspaceMaterializer:
        resolved = settings or get_settings()
        from services.integration.github.factory import build_git_auth_for, build_git_host
        from services.integration.source_control import source_control_repos

        if remote_url_for is None:
            remote_url_for = build_git_host(resolved).source_location
        return cls(
            registry=registry,
            allowed_repos=frozenset(source_control_repos(resolved)),
            workspace_root=Path(resolved.execution_workspace_root),
            remote_url_for=remote_url_for,
            auth_for=auth_for or build_git_auth_for(resolved),
        )

    def _auth(self, repo: str) -> GitAuth:
        return self.auth_for(repo) if self.auth_for is not None else NO_AUTH

    def _guard(self, repo: str) -> None:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)

    def _path_for(self, repo: str, task_id: str, execution_id: str) -> Path:
        return self.workspace_root / _slug(repo) / _slug(task_id) / _slug(execution_id)

    async def materialize(
        self,
        *,
        repo: str,
        task_id: str,
        execution_id: str,
        base: str,
        commit_sha: str,
    ) -> MaterializedWorkspace:
        self._guard(repo)
        branch = agent_branch_name(task_id)
        path = self._path_for(repo, task_id, execution_id)
        await self._ensure_checkout(repo, path, branch, commit_sha)
        head = await _git("rev-parse", "HEAD", cwd=str(path))
        branch_orphan_id = await self.registry.register(
            OrphanArtifact(
                BRANCH,
                f"{repo}:{branch}",
                repository=repo,
                owning_workflow=task_id,
            )
        )
        workspace_orphan_id = await self.registry.register(
            OrphanArtifact(
                STAGING,
                str(path),
                repository=repo,
                owning_workflow=execution_id,
            )
        )
        return MaterializedWorkspace(
            repo=repo,
            task_id=task_id,
            execution_id=execution_id,
            branch=branch,
            base=base,
            commit_sha=head,
            path=str(path),
            branch_orphan_id=branch_orphan_id,
            workspace_orphan_id=workspace_orphan_id,
        )

    async def _ensure_checkout(
        self,
        repo: str,
        path: Path,
        branch: str,
        commit_sha: str,
    ) -> None:
        auth = self._auth(repo)
        reused = (path / ".git").is_dir()
        if reused:
            head = await _git("rev-parse", "HEAD", cwd=str(path))
            current = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=str(path))
            if head == commit_sha and current == branch:
                return
            if not await _git_ok("cat-file", "-e", f"{commit_sha}^{{commit}}", cwd=str(path)):
                await _git("fetch", "--quiet", "origin", branch, cwd=str(path), auth=auth)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            await _git("clone", "--quiet", "--", self.remote_url_for(repo), str(path), auth=auth)
        await _git(
            "-c",
            "advice.detachedHead=false",
            "checkout",
            "-f",
            "-B",
            branch,
            commit_sha,
            cwd=str(path),
        )
        if reused:
            await _git("reset", "--hard", commit_sha, cwd=str(path))
            await _git("clean", "-fd", cwd=str(path))

    async def changed_paths(self, workspace: MaterializedWorkspace) -> list[str]:
        """Repository-relative paths the workspace has changed, deletions included.

        `-z` is what makes a path outside ASCII usable, because `core.quotePath` would otherwise
        C-quote it into something no caller can open.
        """
        out = await _git("status", "--porcelain=v1", "-z", "--no-renames", cwd=str(workspace.path))
        paths: list[str] = []
        for entry in out.split("\0"):
            if len(entry) > 3:
                paths.append(entry[3:])
        return sorted(paths)

    async def diff_paths_against(self, workspace: MaterializedWorkspace, base: str) -> list[str]:
        """Repository-relative paths by which the workspace differs from `base`.

        The comparison spans the branch commits and the uncommitted worktree together, which is
        what answers whether any of the task's work survives at this moment.
        """
        out = await _git(
            "diff", "--name-only", "-z", "--no-renames", base, cwd=str(workspace.path)
        )
        return sorted(entry for entry in out.split("\0") if entry)

    async def stage(
        self, workspace: MaterializedWorkspace, *, allowed_paths: Sequence[str] = ()
    ) -> StagedChange:
        """Stage the workspace, limited to `allowed_paths` when the ticket declared any.

        Build tooling rewrites tracked files the ticket never asked for, so an unrestricted
        `git add -A` publishes whatever the workspace happens to hold. Declared paths are
        intersected with the changed set before reaching git, because `git add -A -- <path>`
        aborts on a pathspec that matches nothing. The index is reset to HEAD first, since the
        implementation activity stages this same workspace before the commit activity runs.
        """
        path = str(workspace.path)
        if not allowed_paths:
            await _git("add", "-A", cwd=path)
            excluded: tuple[str, ...] = ()
        else:
            changed = await self.changed_paths(workspace)
            admitted = [p for p in changed if _within(p, allowed_paths)]
            excluded = tuple(p for p in changed if p not in set(admitted))
            await _git("read-tree", "HEAD", cwd=path)
            if admitted:
                await _git("add", "-A", "--", *admitted, cwd=path)
        tree = await _git("write-tree", cwd=path)
        head_tree = await _git("rev-parse", "HEAD^{tree}", cwd=path)
        return StagedChange(tree=tree, changed=tree != head_tree, excluded=excluded)

    async def build_commit_request(
        self,
        workspace: MaterializedWorkspace,
        *,
        message: str,
        staged: StagedChange | None = None,
        allowed_paths: Sequence[str] = (),
    ) -> CommitRequest:
        resolved = staged
        if resolved is None:
            resolved = await self.stage(workspace, allowed_paths=allowed_paths)
        return CommitRequest(
            repo=workspace.repo,
            branch=workspace.branch,
            base=workspace.base,
            message=message,
            workspace=str(workspace.path),
            tree=resolved.tree,
            task_id=workspace.task_id,
            execution_id=workspace.execution_id,
        )
