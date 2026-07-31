"""Three-way drift detection for the agent branch against a bare git remote."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from services.orchestration.branch_sync import (
    CONFLICTS,
    FAST_FORWARD,
    REBASED,
    GitBranchSynchronizer,
)

REPO = "agentic-demo-service"
BRANCH = "agent/AGENTDEV-1/exec-1"


def _git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _identity() -> list[str]:
    return ["-c", "user.email=seed@example.invalid", "-c", "user.name=seed"]


@pytest.fixture
def remote(tmp_path) -> tuple[str, str]:
    path = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(path), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    (work / "shared.py").write_text("value = 0\n", encoding="utf-8")
    _git(["add", "."], str(work))
    _git([*_identity(), "commit", "-q", "-m", "init"], str(work))
    _git(["push", "-q", "origin", "main"], str(work))
    head = _git(["rev-parse", "HEAD"], str(work))
    return str(path), head


def _advance_base(remote_path: str, tmp_path: Path, path: str, content: str) -> str:
    work = tmp_path / f"advance-{path.replace('/', '_')}"
    subprocess.run(["git", "clone", "-q", remote_path, str(work)], check=True, capture_output=True)
    (work / path).write_text(content, encoding="utf-8")
    _git(["add", "."], str(work))
    _git([*_identity(), "commit", "-q", "-m", f"advance {path}"], str(work))
    _git(["push", "-q", "origin", "main"], str(work))
    return _git(["rev-parse", "HEAD"], str(work))


def _workspace(remote_path: str, tmp_path: Path) -> Path:
    work = tmp_path / "workspace"
    subprocess.run(["git", "clone", "-q", remote_path, str(work)], check=True, capture_output=True)
    return work


def _synchronizer(remote_path: str) -> GitBranchSynchronizer:
    return GitBranchSynchronizer(
        allowed_repos=frozenset({REPO}),
        remote_url_for=lambda repo: remote_path,
    )


async def test_unchanged_base_fast_forwards(remote, tmp_path) -> None:
    remote_path, base_sha = remote
    workspace = _workspace(remote_path, tmp_path)
    (workspace / "feature.py").write_text("added = True\n", encoding="utf-8")

    outcome = await _synchronizer(remote_path).synchronize(
        repo=REPO,
        base="main",
        base_sha=base_sha,
        branch=BRANCH,
        workspace_path=str(workspace),
        resolved_files={},
    )

    assert outcome.status == FAST_FORWARD
    assert outcome.base_head == base_sha
    assert outcome.conflicts == []


async def test_advanced_base_without_overlap_rebases(remote, tmp_path) -> None:
    remote_path, base_sha = remote
    base_head = _advance_base(remote_path, tmp_path, "docs.md", "notes\n")
    workspace = _workspace(remote_path, tmp_path)
    (workspace / "feature.py").write_text("added = True\n", encoding="utf-8")

    outcome = await _synchronizer(remote_path).synchronize(
        repo=REPO,
        base="main",
        base_sha=base_sha,
        branch=BRANCH,
        workspace_path=str(workspace),
        resolved_files={},
    )

    assert outcome.status == REBASED
    assert outcome.base_head == base_head
    assert outcome.conflicts == []


async def test_advanced_base_with_overlap_reports_conflict(remote, tmp_path) -> None:
    remote_path, base_sha = remote
    base_head = _advance_base(remote_path, tmp_path, "shared.py", "value = 1\n")
    workspace = _workspace(remote_path, tmp_path)
    (workspace / "shared.py").write_text("value = 2\n", encoding="utf-8")

    outcome = await _synchronizer(remote_path).synchronize(
        repo=REPO,
        base="main",
        base_sha=base_sha,
        branch=BRANCH,
        workspace_path=str(workspace),
        resolved_files={},
    )

    assert outcome.status == CONFLICTS
    assert outcome.base_head == base_head
    assert len(outcome.conflicts) == 1
    conflict = outcome.conflicts[0]
    assert conflict.path == "shared.py"
    assert conflict.ours == "value = 1\n"
    assert conflict.theirs == "value = 2\n"
    assert conflict.base == "value = 0\n"


async def test_resolved_path_is_excluded_from_conflicts(remote, tmp_path) -> None:
    remote_path, base_sha = remote
    base_head = _advance_base(remote_path, tmp_path, "shared.py", "value = 1\n")
    workspace = _workspace(remote_path, tmp_path)
    (workspace / "shared.py").write_text("value = 2\n", encoding="utf-8")

    outcome = await _synchronizer(remote_path).synchronize(
        repo=REPO,
        base="main",
        base_sha=base_sha,
        branch=BRANCH,
        workspace_path=str(workspace),
        resolved_files={"shared.py": "value = 3\n"},
    )

    assert outcome.status == REBASED
    assert outcome.base_head == base_head
    assert outcome.conflicts == []
