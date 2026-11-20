"""Base-branch drift detection and three-way conflict extraction for the agent branch."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts.config import Settings, get_settings

from services import process
from services.integration.github.git_auth import NO_AUTH, GitAuth, no_auth_for
from services.integration.github.mediator import RepoNotAllowedError

FAST_FORWARD = "fast_forward"
REBASED = "rebased"
CONFLICTS = "conflicts"

_AGENT_EMAIL = "agent-platform@users.noreply.invalid"


@dataclass(frozen=True)
class ConflictPayload:
    path: str
    ours: str
    theirs: str
    base: str = ""


@dataclass(frozen=True)
class SyncOutcome:
    status: str
    base_head: str
    human_commits: bool = False
    conflicts: list[ConflictPayload] = field(default_factory=list)


def _run_git(args: list[str], cwd: str, auth: GitAuth = NO_AUTH) -> process.CompletedProcess[str]:
    return process.run(
        ["git", "-c", "safe.directory=*", *auth.config_args(), *args],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **auth.env()},
    )


async def _git(*args: str, cwd: str, auth: GitAuth = NO_AUTH) -> str:
    proc = await asyncio.to_thread(_run_git, list(args), cwd, auth)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout.rstrip()


async def _show(ref: str, path: str, *, cwd: str) -> str:
    proc = await asyncio.to_thread(_run_git, ["show", f"{ref}:{path}"], cwd)
    return proc.stdout if proc.returncode == 0 else ""


class BranchSynchronizer(Protocol):
    async def synchronize(
        self,
        *,
        repo: str,
        base: str,
        base_sha: str,
        branch: str,
        workspace_path: str,
        resolved_files: Mapping[str, str],
    ) -> SyncOutcome: ...


@dataclass
class GitBranchSynchronizer:
    allowed_repos: frozenset[str]
    remote_url_for: Callable[[str], str]
    agent_email: str = _AGENT_EMAIL
    auth_for: Callable[[str], GitAuth] = no_auth_for

    @classmethod
    def from_settings(
        cls,
        *,
        remote_url_for: Callable[[str], str] | None = None,
        auth_for: Callable[[str], GitAuth] | None = None,
        settings: Settings | None = None,
    ) -> GitBranchSynchronizer:
        from services.integration.github.factory import build_git_auth_for
        from services.integration.source_control import source_control_repos

        resolved = settings or get_settings()
        return cls(
            allowed_repos=frozenset(source_control_repos(resolved)),
            remote_url_for=remote_url_for or (lambda repo: repo),
            auth_for=auth_for or build_git_auth_for(resolved),
        )

    async def synchronize(
        self,
        *,
        repo: str,
        base: str,
        base_sha: str,
        branch: str,
        workspace_path: str,
        resolved_files: Mapping[str, str],
    ) -> SyncOutcome:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)
        cwd = str(Path(workspace_path))
        auth = self.auth_for(repo)
        await _git("fetch", "--quiet", "origin", base, cwd=cwd, auth=auth)
        base_head = await _git("rev-parse", "FETCH_HEAD", cwd=cwd)
        human_commits = await self._human_commits(branch, base_sha, cwd=cwd, auth=auth)
        if base_head == base_sha:
            return SyncOutcome(FAST_FORWARD, base_head, human_commits)

        agent_files = await self._agent_files(cwd=cwd)
        agent_files.update(resolved_files)
        base_changed = set(
            (await _git("diff", "--name-only", base_sha, base_head, cwd=cwd)).splitlines()
        )
        conflicts: list[ConflictPayload] = []
        for path in sorted(agent_files):
            if path in resolved_files or path not in base_changed:
                continue
            ours = await _show(base_head, path, cwd=cwd)
            theirs = agent_files[path]
            if theirs.strip() == ours.strip():
                continue
            conflicts.append(
                ConflictPayload(
                    path=path,
                    ours=ours,
                    theirs=theirs,
                    base=await _show(base_sha, path, cwd=cwd),
                )
            )
        status = CONFLICTS if conflicts else REBASED
        return SyncOutcome(status, base_head, human_commits, conflicts)

    async def _agent_files(self, *, cwd: str) -> dict[str, str]:
        out = await _git("status", "--porcelain=v1", "--no-renames", cwd=cwd)
        files: dict[str, str] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            code, rel = line[:2], line[3:].strip()
            if "D" in code:
                continue
            candidate = Path(cwd) / rel
            if candidate.is_file():
                files[rel] = candidate.read_text(encoding="utf-8")
        return files

    async def _human_commits(
        self, branch: str, base_sha: str, *, cwd: str, auth: GitAuth = NO_AUTH
    ) -> bool:
        remote = await _git("ls-remote", "--heads", "origin", branch, cwd=cwd, auth=auth)
        if not remote:
            return False
        await _git("fetch", "--quiet", "origin", branch, cwd=cwd, auth=auth)
        emails = await _git("log", f"{base_sha}..FETCH_HEAD", "--format=%ae", cwd=cwd)
        return any(
            line.strip() and line.strip() != self.agent_email for line in emails.splitlines()
        )
