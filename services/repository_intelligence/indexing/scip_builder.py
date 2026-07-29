"""Generate and cache SCIP indexes for a repository, keyed by repo and commit."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from services.repository_intelligence.indexing.scip_index import ScipIndex

DEFAULT_SCIP_PYTHON_CMD = "npx --yes @sourcegraph/scip-python@0.6.6"
DEFAULT_SCIP_CLI = "scip"

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]


class ScipBuildError(RuntimeError):
    """Raised when SCIP index generation fails."""


def default_index_root() -> Path:
    """Storage root for generated SCIP indexes (gitignored build artifacts)."""
    return _PLATFORM_ROOT / "repository_indexes" / "scip" / "index"


def resolve_commit(repo_root: Path) -> str:
    """Return the current commit SHA of the repository at repo_root."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def index_dir(project: str, commit: str, index_root: Path | None = None) -> Path:
    """Return the commit-pinned directory that holds a project's SCIP artifacts."""
    root = index_root or default_index_root()
    return root / f"{project}@{commit[:7]}"


def build_scip_index(
    repo_root: str | Path,
    *,
    project: str | None = None,
    commit: str | None = None,
    index_root: Path | None = None,
    scip_python_cmd: str = DEFAULT_SCIP_PYTHON_CMD,
    scip_cli: str = DEFAULT_SCIP_CLI,
    refresh: bool = False,
) -> tuple[ScipIndex, str]:
    """Build or load the cached SCIP index for repo_root and return it with its commit."""
    root = Path(repo_root).resolve()
    project_name = project or root.name
    commit_sha = commit or resolve_commit(root)
    target = index_dir(project_name, commit_sha, index_root)
    json_path = target / "index.scip.json"

    if refresh or not json_path.exists():
        target.mkdir(parents=True, exist_ok=True)
        scip_path = target / "index.scip"
        _generate_scip(root, project_name, commit_sha[:7], scip_python_cmd, scip_path)
        _print_scip_json(scip_cli, scip_path, json_path)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    return ScipIndex.from_scip_json(data, project_name), commit_sha


def _generate_scip(
    repo_root: Path, project: str, version: str, scip_python_cmd: str, output: Path
) -> None:
    try:
        subprocess.run(
            [
                *shlex.split(scip_python_cmd),
                "index",
                "--project-name",
                project,
                "--project-version",
                version,
                "--output",
                str(output),
                "--quiet",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=_index_env(repo_root),
        )
    except subprocess.CalledProcessError as error:
        raise ScipBuildError(f"scip-python index failed: {error.stderr.strip()}") from error


def _index_env(repo_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    venv_bin = repo_root / ".venv" / "bin"
    if venv_bin.is_dir():
        env["VIRTUAL_ENV"] = str(repo_root / ".venv")
        env["PATH"] = os.pathsep.join([str(venv_bin), env.get("PATH", "")])
    return env


def _print_scip_json(scip_cli: str, scip_path: Path, json_path: Path) -> None:
    try:
        completed = subprocess.run(
            [scip_cli, "print", "--json", str(scip_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise ScipBuildError(f"scip print failed: {error.stderr.strip()}") from error
    json_path.write_text(completed.stdout, encoding="utf-8")
