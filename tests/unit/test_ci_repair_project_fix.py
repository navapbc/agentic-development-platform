"""The lint-repair path runs the repo's own declared fix command and keeps only verified edits."""

from __future__ import annotations

import asyncio
from pathlib import Path

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

    applied = _apply_staged_files(str(staging), str(workspace), ("portal/generated.lock",), ())

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

    applied = _apply_staged_files(str(staging), str(workspace), ("../outside.txt",), ())

    assert applied == 0
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "secret"


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
