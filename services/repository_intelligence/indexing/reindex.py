"""Reindex a repository at a commit, reusing unchanged backends and publishing atomically."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from services import process
from services.repository_intelligence.indexing import scip_builder, zoekt_builder
from services.repository_intelligence.indexing.scip_builder import ScipIndexer
from services.repository_intelligence.normalization.models import RepoRef


def changed_paths(
    repo_root: str | Path, base_commit: str | None, target_commit: str
) -> tuple[str, ...] | None:
    """Return files differing between two commits, or None if the diff is unavailable."""
    if not base_commit:
        return None
    try:
        completed = process.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--name-only",
                f"{base_commit}..{target_commit}",
            ],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            check=True,
            capture_output=True,
            text=True,
        )
    except (process.CalledProcessError, OSError):
        return None
    return tuple(line for line in completed.stdout.splitlines() if line)


def reindex_repo(
    repo_root: str | Path,
    repo: RepoRef,
    *,
    prior_commit: str | None = None,
    index_root: Path | None = None,
    indexer: ScipIndexer = scip_builder.SCIP_PYTHON_INDEXER,
    source_suffixes: tuple[str, ...] | None = None,
) -> None:
    """Reindex repo at its target commit, reusing SCIP when no source file changed."""
    root = Path(repo_root).resolve()
    project = root.name
    target_sha = repo.commit_sha
    changed = changed_paths(root, prior_commit, target_sha)
    suffixes = source_suffixes if source_suffixes is not None else indexer.source_suffixes

    scip_root = index_root or scip_builder.default_index_root()
    reuse_scip = (
        changed is not None
        and prior_commit is not None
        and not _touches(changed, suffixes)
        and _seed(
            scip_builder.index_dir(project, prior_commit, scip_root),
            scip_builder.index_dir(project, target_sha, scip_root),
        )
    )
    if not reuse_scip:
        _atomic_build(
            lambda staging: scip_builder.build_scip_index(
                root, commit=target_sha, index_root=staging, indexer=indexer, refresh=True
            ),
            index_dir=scip_builder.index_dir(project, target_sha, scip_root),
            real_root=scip_root,
        )

    zoekt_root = index_root or zoekt_builder.default_index_root()
    _atomic_build(
        lambda staging: zoekt_builder.build_zoekt_index(
            root, commit=target_sha, index_root=staging, refresh=True
        ),
        index_dir=zoekt_builder.index_dir(project, target_sha, zoekt_root),
        real_root=zoekt_root,
    )


def _touches(paths: tuple[str, ...], suffixes: tuple[str, ...]) -> bool:
    return any(path.endswith(suffixes) for path in paths)


def _atomic_build(build: Callable[[Path], object], *, index_dir: Path, real_root: Path) -> None:
    real_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(dir=real_root, prefix=".staging-"))
    try:
        build(staging_root)
        staged = staging_root / index_dir.name
        _publish(staged, index_dir)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _seed(source: Path, target: Path) -> bool:
    if source == target:
        return target.exists()
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(dir=target.parent, prefix=".seed-"))
    try:
        staged = staging_root / target.name
        shutil.copytree(source, staged)
        _publish(staged, target)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return True


def _publish(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        os.replace(staged, target)
        return
    for entry in staged.iterdir():
        dest = target / entry.name
        if entry.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(entry), str(dest))
        else:
            os.replace(entry, dest)
