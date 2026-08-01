"""Deterministic execution-workspace materialization and branch registration."""

from __future__ import annotations

import asyncio
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from adapters.interfaces.git_host import CommitRequest, agent_branch_name
from scripts.config import Settings, get_settings

from services.coordination.orphans import BRANCH, STAGING, OrphanArtifact, OrphanRegistry
from services.integration.github.mediator import RepoNotAllowedError

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


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


def _default_remote_url(settings: Settings, repo: str) -> str:
    base = settings.github_git_base_url.rstrip("/")
    if settings.github_token and base.startswith("https://"):
        base = f"https://x-access-token:{settings.github_token}@{base[len('https://') :]}"
    return f"{base}/{settings.github_owner}/{repo}.git"


def _run_git(args: list[str], cwd: str | None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.rstrip()


async def _git(*args: str, cwd: str | None = None) -> str:
    return await asyncio.to_thread(_run_git, list(args), cwd)


@dataclass
class WorkspaceMaterializer:
    registry: OrphanRegistry
    allowed_repos: frozenset[str]
    workspace_root: Path
    remote_url_for: Callable[[str], str]

    @classmethod
    def from_settings(
        cls,
        registry: OrphanRegistry,
        *,
        remote_url_for: Callable[[str], str] | None = None,
        settings: Settings | None = None,
    ) -> WorkspaceMaterializer:
        resolved = settings or get_settings()
        return cls(
            registry=registry,
            allowed_repos=frozenset(resolved.allowed_repos()),
            workspace_root=Path(resolved.execution_workspace_root),
            remote_url_for=remote_url_for or (lambda repo: _default_remote_url(resolved, repo)),
        )

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
        if (path / ".git").is_dir():
            head = await _git("rev-parse", "HEAD", cwd=str(path))
            current = await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=str(path))
            if head == commit_sha and current == branch:
                return
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            await _git("clone", "--quiet", self.remote_url_for(repo), str(path))
        await _git(
            "-c",
            "advice.detachedHead=false",
            "checkout",
            "-B",
            branch,
            commit_sha,
            cwd=str(path),
        )

    async def collect_changes(self, workspace: MaterializedWorkspace) -> dict[str, str]:
        path = Path(workspace.path)
        out = await _git("status", "--porcelain=v1", "--no-renames", cwd=str(path))
        files: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            code, rel = line[:2], line[3:].strip()
            if "D" in code:
                continue
            candidate = path / rel
            if candidate.is_file():
                files[rel] = candidate.read_text(encoding="utf-8")
        return files

    async def build_commit_request(
        self,
        workspace: MaterializedWorkspace,
        *,
        message: str,
        files: Mapping[str, str] | None = None,
    ) -> CommitRequest:
        resolved = dict(files) if files is not None else await self.collect_changes(workspace)
        return CommitRequest(
            repo=workspace.repo,
            branch=workspace.branch,
            base=workspace.base,
            message=message,
            files=resolved,
            task_id=workspace.task_id,
            execution_id=workspace.execution_id,
        )
