from __future__ import annotations

import subprocess
from pathlib import Path

from services.integration.source_control import source_control_repos
from tests.support import hermetic_settings


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_repo(root: Path, name: str) -> None:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")


def test_local_backend_reads_declared_repos() -> None:
    settings = hermetic_settings(
        source_control_backend="local",
        source_control_local_repos="agentic-demo-service, agentic-demo-greenfield",
    )
    assert source_control_repos(settings) == {
        "agentic-demo-service",
        "agentic-demo-greenfield",
    }


def test_local_backend_discovers_repos_under_root(tmp_path: Path) -> None:
    _make_repo(tmp_path, "svc")
    _make_repo(tmp_path, "web")
    (tmp_path / "not-a-repo").mkdir()
    settings = hermetic_settings(
        source_control_backend="local",
        source_control_local_root=str(tmp_path),
    )
    assert source_control_repos(settings) == {"svc", "web"}


def test_local_backend_declared_overrides_discovery(tmp_path: Path) -> None:
    _make_repo(tmp_path, "svc")
    settings = hermetic_settings(
        source_control_backend="local",
        source_control_local_root=str(tmp_path),
        source_control_local_repos="only-this",
    )
    assert source_control_repos(settings) == {"only-this"}


def test_local_backend_needs_no_github_config(tmp_path: Path) -> None:
    settings = hermetic_settings(
        source_control_backend="local",
        source_control_local_repos="svc",
        environment="nonexistent-env",
    )
    assert source_control_repos(settings) == {"svc"}
