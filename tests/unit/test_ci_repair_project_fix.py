"""The lint-repair path runs the repo's own declared fix command and keeps only verified edits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from services.orchestration.checks.runner import (
    ChecksFixResult,
    ChecksSpec,
    _fix_inner_script,
    checks_pod_name,
    parse_fix_output,
)
from services.orchestration.temporal_activities.repair import (
    ApplyCIRepairRequest,
    _apply_staged_files,
)


def _spec(**overrides) -> ChecksSpec:
    base = {
        "repo": "org/app",
        "execution_id": "exec-1",
        "checkout_dir": "/checkout",
        "command": "npm run lint",
        "fix_command": "npm ci && npm run format",
        "image": "node:24.16.0-bookworm",
        "build": "",
        "working_dir": "portal",
    }
    base.update(overrides)
    return ChecksSpec(**base)


def test_fix_script_runs_the_declared_command_then_reverifies_with_the_check():
    script = _fix_inner_script(_spec())

    assert script.index("npm ci && npm run format") < script.index("npm run lint")
    assert script.endswith("exit $verify_rc")


def test_fix_script_excludes_dependency_and_tool_cache_trees_from_the_changed_file_manifest():
    script = _fix_inner_script(_spec())

    for pruned in (
        "node_modules",
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
    ):
        assert f"-name {pruned}" in script
    assert "-prune" in script


def test_parse_fix_output_separates_the_manifest_from_the_log():
    files, output = parse_fix_output(
        "installing\n"
        "GATEFIX_FILES_BEGIN\n./portal/src/a.tsx\n./portal/src/b.tsx\nGATEFIX_FILES_END\n"
        "lint clean\nGATEFIX_VERIFY_RC=0"
    )

    assert files == ("portal/src/a.tsx", "portal/src/b.tsx")
    assert output == "installing\nlint clean"


async def test_run_fix_reports_unverified_when_no_fix_command_is_declared():
    from services.orchestration.checks.runner import ChecksRunner

    result = await ChecksRunner(object()).run_fix(_spec(fix_command=""), "/staging")

    assert result == ChecksFixResult(
        verified=False, exit_code=1, output="no fix_command declared"
    )


def test_apply_staged_files_copies_only_files_the_failure_named(tmp_path):
    staging, workspace = tmp_path / "staging", tmp_path / "workspace"
    for root in (staging, workspace):
        (root / "portal/src").mkdir(parents=True)
        (root / "api").mkdir(parents=True)
    (staging / "portal/src/a.tsx").write_text("fixed", encoding="utf-8")
    (workspace / "portal/src/a.tsx").write_text("broken", encoding="utf-8")
    (staging / "api/unrelated.py").write_text("touched", encoding="utf-8")
    (workspace / "api/unrelated.py").write_text("original", encoding="utf-8")

    applied = _apply_staged_files(
        str(staging),
        str(workspace),
        ("portal/src/a.tsx", "api/unrelated.py"),
        ("src/a.tsx",),
    )

    assert applied == 1
    assert (workspace / "portal/src/a.tsx").read_text(encoding="utf-8") == "fixed"
    assert (workspace / "api/unrelated.py").read_text(encoding="utf-8") == "original"


def test_apply_staged_files_ignores_files_the_fix_command_created(tmp_path):
    staging, workspace = tmp_path / "staging", tmp_path / "workspace"
    (staging / "portal").mkdir(parents=True)
    workspace.mkdir()
    (staging / "portal/generated.lock").write_text("new", encoding="utf-8")

    applied = _apply_staged_files(
        str(staging), str(workspace), ("portal/generated.lock",), ("portal/generated.lock",)
    )

    assert applied == 0
    assert not (Path(workspace) / "portal/generated.lock").exists()


def test_manifest_preserves_dot_directories_and_dotfiles():
    files, _ = parse_fix_output(
        "GATEFIX_FILES_BEGIN\n./.github/workflows/ci.yml\n./portal/.eslintrc.js\nGATEFIX_FILES_END"
    )

    assert files == (".github/workflows/ci.yml", "portal/.eslintrc.js")


def test_apply_staged_files_refuses_entries_that_escape_the_staging_root(tmp_path):
    staging, workspace = tmp_path / "staging", tmp_path / "workspace"
    staging.mkdir()
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    applied = _apply_staged_files(
        str(staging), str(workspace), ("../outside.txt",), ("../outside.txt",)
    )

    assert applied == 0
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "secret"


def test_apply_staged_files_copies_nothing_when_the_failure_named_no_path(tmp_path):
    """An empty scope is fail-closed, so a broad fix command cannot rewrite the tree."""
    staging, workspace = tmp_path / "staging", tmp_path / "workspace"
    for root in (staging, workspace):
        (root / "portal/src").mkdir(parents=True)
    (staging / "portal/src/a.tsx").write_text("reformatted", encoding="utf-8")
    (workspace / "portal/src/a.tsx").write_text("original", encoding="utf-8")

    applied = _apply_staged_files(
        str(staging), str(workspace), ("portal/src/a.tsx",), ()
    )

    assert applied == 0
    assert (workspace / "portal/src/a.tsx").read_text(encoding="utf-8") == "original"


def test_autofix_degrades_to_agent_repair_when_the_pod_transport_fails(monkeypatch):
    from services.orchestration.temporal_activities import repair as repair_module

    def explode(*args, **kwargs):
        raise RuntimeError("kubectl not found")

    monkeypatch.setattr(repair_module, "_build_materializer", explode, raising=False)
    request = ApplyCIRepairRequest(
        task_id="t", execution_id="e", repo="o/r", base="main", commit="c",
        instruction="fix", failure_summary="portal/src/a.tsx broken",
    )

    assert asyncio.run(repair_module._project_autofix(request, "/nonexistent")) is False


def test_fix_and_check_pods_of_one_execution_get_distinct_names():
    repo = "department-of-example/benefits-eligibility-portal"
    execution = "b3f1c2d4e5a64f708192a3b4c5d6e7f8"

    checks = checks_pod_name(repo, execution)
    fix = checks_pod_name(repo, f"{execution}-fix")

    assert checks != fix
    assert len(checks) <= 63 and len(fix) <= 63


def test_concurrent_executions_of_one_repo_get_distinct_pod_names():
    repo = "department-of-example/benefits-eligibility-portal"

    assert checks_pod_name(repo, "execution-one") != checks_pod_name(repo, "execution-two")


_RAISE = object()


@pytest.mark.parametrize(
    "fix_command,exit_code,changed,summary,applied,expected",
    [
        ("", 0, (), "portal/a.tsx broken", 0, "no-fix-command"),
        ("npm run fix", _RAISE, (), "portal/a.tsx broken", 0, "pod-failed"),
        ("npm run fix", 1, (), "portal/a.tsx broken", 0, "fix-exited-non-zero"),
        ("npm run fix", 0, (), "portal/a.tsx broken", 0, "fix-changed-nothing"),
        ("npm run fix", 0, ("portal/a.tsx",), "code style issues found", 0, "empty-scope"),
        ("npm run fix", 0, ("portal/a.tsx",), "portal/a.tsx broken", 0, "nothing-applied"),
        ("npm run fix", 0, ("portal/a.tsx",), "portal/a.tsx broken", 1, "applied"),
    ],
)
def test_project_autofix_names_every_outcome(
    monkeypatch, fix_command, exit_code, changed, summary, applied, expected
):
    """Each row of the discriminator table resolves to exactly one outcome name."""
    from services.orchestration.checks import config as checks_config
    from services.orchestration.checks import runner as checks_runner
    from services.orchestration.gates import pod_runner
    from services.orchestration.temporal_activities import checks as checks_activity
    from services.orchestration.temporal_activities import repair as repair_module

    class _Config:
        command = "npm run lint"
        image = "node:24"
        build = ""
        working_dir = "portal"

    _Config.fix_command = fix_command

    class _Runner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run_fix(self, spec, staging):
            if exit_code is _RAISE:
                raise RuntimeError("kubectl not found")
            return ChecksFixResult(
                verified=exit_code == 0 and bool(changed),
                exit_code=exit_code,
                output="",
                changed_files=changed,
            )

    recorded: list[str] = []
    monkeypatch.setattr(checks_config, "checks_for_repo", lambda *a, **k: _Config())
    monkeypatch.setattr(checks_runner, "ChecksRunner", _Runner)
    monkeypatch.setattr(pod_runner, "KubectlPodOps", lambda *a, **k: object())
    monkeypatch.setattr(checks_activity, "_clean_checkout", lambda *a, **k: None)
    monkeypatch.setattr(repair_module, "_apply_staged_files", lambda *a, **k: applied)
    monkeypatch.setattr(
        repair_module, "_record_fix_outcome", lambda name, **f: recorded.append(name)
    )

    request = ApplyCIRepairRequest(
        task_id="t", execution_id="e", repo="org/app", base="main", commit="c",
        instruction="fix", failure_summary=summary,
    )
    result = asyncio.run(repair_module._project_autofix(request, "/workspace"))

    assert recorded == [expected]
    assert result is (expected == "applied")


class _RecordingPodOps:
    """A pod transport that records the lifecycle and returns a scripted fix result."""

    def __init__(self, exit_code: int = 0, stdout: str = "") -> None:
        self.created: list[dict] = []
        self.copied_in: list[tuple[str, str]] = []
        self.copied_out: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self._exit_code = exit_code
        self._stdout = stdout

    async def create(self, manifest):
        self.created.append(manifest)

    async def wait_ready(self, pod, namespace):
        return None

    async def copy_in(self, pod, namespace, source, dest):
        self.copied_in.append((source, dest))

    async def copy_out(self, pod, namespace, source, dest, manifest_path):
        self.copied_out.append((source, dest, manifest_path))

    async def exec_capture(self, pod, namespace, script):
        return self._exit_code, self._stdout, ""

    async def delete(self, pod, namespace):
        self.deleted.append(pod)


def _fix_output(*files: str) -> str:
    listing = "\n".join(f"./{name}" for name in files)
    return f"GATEFIX_FILES_BEGIN\n{listing}\nGATEFIX_FILES_END\nGATEFIX_VERIFY_RC=0"


def test_a_verified_fix_copies_the_changed_files_out_of_the_pod():
    """The happy path must stage the edits, or a verified fix reaches the workspace empty."""
    from services.orchestration.checks.runner import ChecksRunner

    ops = _RecordingPodOps(exit_code=0, stdout=_fix_output("portal/a.ts", "portal/b.ts"))
    runner = ChecksRunner(ops)

    result = asyncio.run(runner.run_fix(_spec(), "/staging"))

    assert result.verified is True
    assert result.changed_files == ("portal/a.ts", "portal/b.ts")
    assert ops.copied_in == [("/checkout", "/workspace")]
    assert ops.copied_out == [("/workspace", "/staging", "/workspace/.gatefix.list")]
    assert ops.deleted, "the fix pod must be torn down"


def test_a_fix_that_changes_nothing_is_unverified_and_copies_nothing_out():
    from services.orchestration.checks.runner import ChecksRunner

    ops = _RecordingPodOps(exit_code=0, stdout=_fix_output())
    runner = ChecksRunner(ops)

    result = asyncio.run(runner.run_fix(_spec(), "/staging"))

    assert result.verified is False
    assert ops.copied_out == [], "nothing changed, so there is nothing to stage"


def test_a_fix_whose_verify_fails_stages_nothing_even_though_files_changed():
    """An unverified edit must not reach the workspace, or the repair lands a broken tree."""
    from services.orchestration.checks.runner import ChecksRunner

    ops = _RecordingPodOps(
        exit_code=2,
        stdout="GATEFIX_FILES_BEGIN\n./portal/a.ts\nGATEFIX_FILES_END\nGATEFIX_VERIFY_RC=2",
    )
    runner = ChecksRunner(ops)

    result = asyncio.run(runner.run_fix(_spec(), "/staging"))

    assert result.verified is False
    assert result.changed_files == ("portal/a.ts",)
    assert ops.copied_out == [], "the verify failed, so the edits stay in the pod"
    assert ops.deleted, "the fix pod must be torn down"


def test_the_fix_script_announces_a_failing_fix_command_so_it_survives_into_the_summary():
    """The script exits on the verify status, so a failing fix command is otherwise silent."""
    script = _fix_inner_script(_spec())

    assert "fix_rc=$?" in script
    assert 'echo "fix command exited $fix_rc"' in script
    assert script.index("fix_rc=$?") < script.index("npm run lint")


def test_a_failing_fix_command_notice_is_kept_in_the_parsed_output():
    files, output = parse_fix_output(
        "fix command exited 127\n"
        "GATEFIX_FILES_BEGIN\nGATEFIX_FILES_END\nGATEFIX_VERIFY_RC=1"
    )

    assert files == ()
    assert "fix command exited 127" in output


def test_a_malformed_checks_config_degrades_the_hint_instead_of_failing_analysis(monkeypatch):
    """Analysis never needed the checks config before, so a bad one must not break it."""
    import scripts.config
    from services.integration.credentials import ConfigError
    from services.orchestration.temporal_activities.repair import (
        AnalyzeCIFailureRequest,
        analyze_ci_failure,
    )

    class _Settings:
        repair_analysis_use_llm = False

    def explode(repo, settings):
        raise ConfigError("checks entry 'org/app' needs a command")

    monkeypatch.setattr(scripts.config, "get_settings", lambda: _Settings())
    monkeypatch.setattr("services.orchestration.checks.config.checks_for_repo", explode)

    result = asyncio.run(
        analyze_ci_failure(
            AnalyzeCIFailureRequest(
                failure_summary="app.py:1:1: E501 line too long, ruff would reformat",
                repo="org/app",
            )
        )
    )

    assert result.actionable is True
    assert "fix command" not in result.instruction
