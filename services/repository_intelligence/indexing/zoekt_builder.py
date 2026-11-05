"""Generate and cache Zoekt search shards for a repository, keyed by repo and commit."""

from __future__ import annotations

import shlex
from pathlib import Path

from services import process

DEFAULT_ZOEKT_INDEX_CMD = "zoekt-index"
DEFAULT_ZOEKT_SEARCH_CMD = "zoekt"

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]

IGNORED_DIRS = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
)

_MARKER = ".indexed"


class ZoektBuildError(RuntimeError):
    """Raised when Zoekt shard generation fails."""


def default_index_root() -> Path:
    """Storage root for generated Zoekt shards (gitignored build artifacts)."""
    return _PLATFORM_ROOT / "repository_indexes" / "zoekt" / "index"


def resolve_commit(repo_root: Path) -> str:
    """Return the current commit SHA of the repository at repo_root."""
    completed = process.run(
        ["git", "rev-parse", "HEAD"],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def index_dir(project: str, commit: str, index_root: Path | None = None) -> Path:
    """Return the commit-pinned directory that holds a project's Zoekt shards."""
    root = index_root or default_index_root()
    return root / f"{project}@{commit[:7]}"


def indexed_commit(shard_dir: Path) -> str | None:
    """Return the full commit SHA recorded for a shard directory, if indexed."""
    marker = shard_dir / _MARKER
    if not marker.exists():
        return None
    return marker.read_text(encoding="utf-8").strip() or None


def build_zoekt_index(
    repo_root: str | Path,
    *,
    project: str | None = None,
    commit: str | None = None,
    index_root: Path | None = None,
    zoekt_index_cmd: str = DEFAULT_ZOEKT_INDEX_CMD,
    ignore_dirs: tuple[str, ...] = IGNORED_DIRS,
    refresh: bool = False,
) -> tuple[Path, str]:
    """Build or reuse the cached Zoekt shards for repo_root and return their dir and commit."""
    root = Path(repo_root).resolve()
    project_name = project or root.name
    commit_sha = commit or resolve_commit(root)
    target = index_dir(project_name, commit_sha, index_root)

    fresh = indexed_commit(target) == commit_sha and any(target.glob("*.zoekt"))
    if refresh or not fresh:
        target.mkdir(parents=True, exist_ok=True)
        for stale in target.glob("*.zoekt"):
            stale.unlink()
        _generate_shards(root, target, zoekt_index_cmd, ignore_dirs)
        (target / _MARKER).write_text(commit_sha, encoding="utf-8")

    return target, commit_sha


def _generate_shards(
    repo_root: Path, target: Path, zoekt_index_cmd: str, ignore_dirs: tuple[str, ...]
) -> None:
    try:
        process.run(
            [
                *shlex.split(zoekt_index_cmd),
                "-index",
                str(target),
                "-ignore_dirs",
                ",".join(ignore_dirs),
                str(repo_root),
            ],
            timeout=process.LONG_COMMAND_TIMEOUT_SECONDS,
            check=True,
            capture_output=True,
            text=True,
        )
    except process.CalledProcessError as error:
        raise ZoektBuildError(f"zoekt-index failed: {error.stderr.strip()}") from error
