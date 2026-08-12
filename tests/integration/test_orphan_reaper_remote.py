"""Hermetic reaper test: delete an expired agent branch on a bare git remote."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from adapters.interfaces.git_host import agent_branch_name
from adapters.real.git_host import RealGitHost
from services.coordination.orphans import (
    BRANCH,
    REAPED,
    InMemoryOrphanRegistry,
    OrphanArtifact,
)
from services.coordination.reaper import DELETE_BRANCH, reap
from services.integration.github.credentials import build_single_identity_resolver

REPO = "agentic-demo-service"
OWNER = "acme"
ALLOWED = frozenset({REPO})
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PAST = NOW - timedelta(hours=1)


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_remote(tmp_path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    identity = ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]
    _git(["add", "."], str(work))
    _git([*identity, "commit", "-q", "-m", "init"], str(work))
    _git(["push", "-q", "origin", "main"], str(work))
    return str(remote)


def _backend(bare_remote: str) -> RealGitHost:
    return RealGitHost(
        build_single_identity_resolver(
            api_url="https://ghes.invalid/api/v3",
            owner=OWNER,
            git_base_url="",
            token="",
            repos={REPO},
        ),
        remote_url_for=lambda repo: bare_remote,
    )


async def _remote_heads(backend: RealGitHost, branch: str) -> str:
    return await backend._ls_remote(backend._remote_url(REPO), branch)


async def test_dry_run_leaves_branch_present(bare_remote: str) -> None:
    backend = _backend(bare_remote)
    branch = agent_branch_name("AGENTDEV-1")
    await backend.ensure_branch(REPO, branch, "main")
    registry = InMemoryOrphanRegistry()
    await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=PAST)
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=True
    )

    assert receipt.actions[0].action == DELETE_BRANCH
    assert receipt.applied == ()
    assert await _remote_heads(backend, branch)


async def test_apply_deletes_branch_on_remote(bare_remote: str) -> None:
    backend = _backend(bare_remote)
    branch = agent_branch_name("AGENTDEV-1")
    await backend.ensure_branch(REPO, branch, "main")
    registry = InMemoryOrphanRegistry()
    orphan_id = await registry.register(
        OrphanArtifact(BRANCH, f"{REPO}:{branch}", repository=REPO, ttl_at=PAST)
    )

    receipt = await reap(
        registry=registry, backend=backend, allowed_repos=ALLOWED, now=NOW, dry_run=False
    )

    assert len(receipt.applied) == 1
    assert not await _remote_heads(backend, branch)
    assert registry.states[orphan_id] == REAPED
