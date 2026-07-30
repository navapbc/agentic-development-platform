"""Generate and cache SCIP indexes for a repository, keyed by repo and commit."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from services.repository_intelligence.indexing.scip_index import ScipIndex
from services.repository_intelligence.languages import Language

DEFAULT_SCIP_PYTHON_CMD = "npx --yes @sourcegraph/scip-python@0.6.6"
DEFAULT_SCIP_TYPESCRIPT_CMD = "npx --yes @sourcegraph/scip-typescript@0.3.14"
DEFAULT_SCIP_CLI = "scip"

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]


class ScipBuildError(RuntimeError):
    """Raised when SCIP index generation fails."""


@dataclass(frozen=True)
class ScipIndexer:
    """A per-language SCIP indexer: its command, source files, and invocation shape."""

    command: str
    language_id: str
    source_suffixes: tuple[str, ...]
    uses_project_flags: bool = False
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    inject_python_venv: bool = False
    infer_tsconfig_when_missing: bool = False

    def index_argv(
        self, project: str, version: str, output: Path, repo_root: Path | None = None
    ) -> list[str]:
        argv = [*shlex.split(self.command), "index"]
        if self.uses_project_flags:
            argv += ["--project-name", project, "--project-version", version]
        argv += ["--output", str(output), *self.extra_args]
        if (
            self.infer_tsconfig_when_missing
            and repo_root is not None
            and not (repo_root / "tsconfig.json").exists()
        ):
            argv.append("--infer-tsconfig")
        return argv

    def index_env(self, repo_root: Path) -> dict[str, str]:
        env = dict(os.environ)
        if self.inject_python_venv:
            venv_bin = repo_root / ".venv" / "bin"
            if venv_bin.is_dir():
                env["VIRTUAL_ENV"] = str(repo_root / ".venv")
                env["PATH"] = os.pathsep.join([str(venv_bin), env.get("PATH", "")])
        return env


SCIP_PYTHON_INDEXER = ScipIndexer(
    command=DEFAULT_SCIP_PYTHON_CMD,
    language_id="python",
    source_suffixes=(".py", ".pyi"),
    uses_project_flags=True,
    extra_args=("--quiet",),
    inject_python_venv=True,
)

SCIP_TYPESCRIPT_INDEXER = ScipIndexer(
    command=DEFAULT_SCIP_TYPESCRIPT_CMD,
    language_id="typescript",
    source_suffixes=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    infer_tsconfig_when_missing=True,
)

_SCIP_INDEXERS: dict[Language, ScipIndexer] = {
    Language.PYTHON: SCIP_PYTHON_INDEXER,
    Language.TYPESCRIPT: SCIP_TYPESCRIPT_INDEXER,
    Language.JAVASCRIPT: SCIP_TYPESCRIPT_INDEXER,
}


def scip_indexer_for(language: Language) -> ScipIndexer | None:
    """Return the SCIP indexer for a language, or None when no indexer is wired."""
    return _SCIP_INDEXERS.get(language)


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
    indexer: ScipIndexer = SCIP_PYTHON_INDEXER,
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
        _generate_scip(root, project_name, commit_sha[:7], indexer, scip_path)
        _print_scip_json(scip_cli, scip_path, json_path)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    return ScipIndex.from_scip_json(data, project_name), commit_sha


def _generate_scip(
    repo_root: Path, project: str, version: str, indexer: ScipIndexer, output: Path
) -> None:
    try:
        subprocess.run(
            indexer.index_argv(project, version, output, repo_root),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=indexer.index_env(repo_root),
        )
    except subprocess.CalledProcessError as error:
        raise ScipBuildError(f"scip index failed: {error.stderr.strip()}") from error


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
