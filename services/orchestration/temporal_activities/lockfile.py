"""Temporal activity that regenerates a change's lockfiles and scans its dependencies.

The agent sandbox reaches no package registry, so an agent that edits a manifest cannot write the
lockfile the project's checks install from. This step runs the profile's lock command in a checks
pod, which reaches the registry, writes the lockfiles it produced back into the workspace, and
scans the changed dependency files before any checks command installs them. The lock command runs
with package scripts disabled and no checks command runs beside it.
"""

from __future__ import annotations

import fnmatch
import posixpath
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity

from services.orchestration.checks.config import ChecksConfig, checks_for_repo
from services.orchestration.checks.runner import ChecksSpec, checks_runner
from services.orchestration.temporal_activities.security import ScanChangeRequest, scan_with

DEPENDENCY_FILES = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "requirements*.txt",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.lockfile",
)


def is_dependency_file(path: str) -> bool:
    name = posixpath.basename(path)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in DEPENDENCY_FILES)


@dataclass
class DependencyStepRequest:
    repo: str
    task_id: str
    execution_id: str
    base: str
    commit: str
    base_ref: str


@dataclass
class DependencyStepResult:
    dependency_files: list[str] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=list)
    lock_failed: bool = False
    lock_detail: str = ""
    blocking_count: int = 0
    threshold: str = ""
    blocking: list[str] = field(default_factory=list)
    evidence_id: str = ""


def _materializer():
    from services.coordination.orphans import PostgresOrphanRegistry
    from services.orchestration.materialization import WorkspaceMaterializer

    return WorkspaceMaterializer.from_settings(PostgresOrphanRegistry())


def dependency_mediator(settings):
    """Trivy and OSV-Scanner, whichever scanner gates the tenant enables.

    Every change to a dependency is scanned, so this step does not read the gates config.
    """
    from services.integration.security.mediator import SecurityScanMediator
    from services.orchestration.gates.builtin import builtin_scanner_backends

    backends = builtin_scanner_backends()
    return SecurityScanMediator.from_settings(
        (backends["trivy"](settings), backends["osv-scanner"](settings)), settings
    )


def _lock_directories(config: ChecksConfig | None, paths: list[str]) -> list[str]:
    if config is None or not config.lock_command:
        return []
    return sorted(
        {posixpath.dirname(p) for p in paths if posixpath.basename(p) in config.lock_files}
    )


def _write_back(
    config: ChecksConfig, staging: str, workspace: str, directories: list[str], written: tuple
) -> list[str]:
    """Copy the lock-pair files the lock command wrote in its directories into the workspace.

    The manifest of the copied files is produced inside the pod, so each path is confined to its
    root and only the files the profile names as its lock pair are taken.
    """
    from services.orchestration.temporal_activities.repair import confined

    staging_root, workspace_root = Path(staging).resolve(), Path(workspace).resolve()
    copied = []
    for entry in written:
        if posixpath.basename(entry) not in config.lock_files:
            continue
        if posixpath.dirname(entry) not in directories:
            continue
        source, target = confined(staging_root, entry), confined(workspace_root, entry)
        if source is None or target is None or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append(entry)
    return sorted(copied)


async def _regenerate(
    req: DependencyStepRequest,
    config: ChecksConfig,
    workspace: str,
    directories: list[str],
    settings,
) -> tuple[list[str], str]:
    """Run the lock command in each directory, returning the lockfiles written and any failure."""
    from services.orchestration.notifications import bounded_detail
    from services.orchestration.temporal_activities.checks import _clean_checkout

    checkout = tempfile.mkdtemp(prefix="lock-")
    staging = tempfile.mkdtemp(prefix="lock-out-")
    try:
        _clean_checkout(workspace, checkout)
        result = await checks_runner(settings).run_lock(
            ChecksSpec(
                repo=req.repo,
                execution_id=req.execution_id,
                checkout_dir=checkout,
                command=config.command,
                image=config.image,
                build=config.build,
                working_dir=config.working_dir,
                lock_command=config.lock_command,
            ),
            directories,
            staging,
        )
        if result.exit_code != 0:
            manifests = ", ".join(
                posixpath.join(d, name) for d in directories for name in config.lock_manifests
            )
            return [], (
                f"`{config.lock_command}` for {manifests} exited {result.exit_code}.\n"
                f"{bounded_detail(result.output)}"
            )
        return _write_back(config, staging, workspace, directories, result.changed_files), ""
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)


@activity.defn
async def prepare_dependencies(req: DependencyStepRequest) -> DependencyStepResult:
    from scripts.config import get_settings

    settings = get_settings()
    materializer = _materializer()
    workspace = await materializer.materialize(
        repo=req.repo,
        task_id=req.task_id,
        execution_id=req.execution_id,
        base=req.base,
        commit_sha=req.commit,
    )
    staged = await materializer.stage(workspace)
    changed = [p for p in staged.paths if is_dependency_file(p)]
    if not changed:
        return DependencyStepResult()
    config = checks_for_repo(req.repo, settings)
    directories = _lock_directories(config, changed)
    lockfiles: list[str] = []
    failure = ""
    if directories and config is not None:
        lockfiles, failure = await _regenerate(req, config, workspace.path, directories, settings)
    scan = await scan_with(
        dependency_mediator(settings),
        ScanChangeRequest(
            task_id=req.task_id,
            execution_id=req.execution_id,
            repo=req.repo,
            ref=req.commit,
            repo_root=workspace.path,
            base_ref=req.base_ref,
        ),
    )
    blocking_ids = set(scan.blocking_finding_ids)
    return DependencyStepResult(
        dependency_files=sorted(set(changed) | set(lockfiles)),
        lockfiles=lockfiles,
        lock_failed=bool(failure),
        lock_detail=failure,
        blocking_count=scan.blocking_count,
        threshold=scan.threshold,
        blocking=[
            f"{f.rule_id} {f.severity} {f.location}"
            for f in scan.findings
            if f.finding_id in blocking_ids
        ],
        evidence_id=scan.evidence_id,
    )
