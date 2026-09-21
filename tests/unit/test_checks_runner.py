import pytest
from services.integration.credentials import ConfigError
from services.orchestration.checks.config import (
    ChecksConfig,
    parse_checks_config,
    parse_checks_configs,
)
from services.orchestration.checks.runner import (
    ChecksFixResult,
    ChecksResult,
    ChecksRunner,
    ChecksSpec,
    _checks_script,
    _fix_inner_script,
    checks_pod_name,
)

pytestmark = pytest.mark.asyncio


def test_parse_checks_config_image_mode():
    cfg = parse_checks_config(
        "repo-a", {"image": "python:3.12-slim", "command": "pytest -q", "timeout_minutes": 10}
    )
    assert cfg == ChecksConfig(
        repo="repo-a",
        image="python:3.12-slim",
        build="",
        command="pytest -q",
        working_dir="/workspace",
        timeout_minutes=10,
    )


def test_parse_checks_config_requires_command():
    with pytest.raises(ConfigError):
        parse_checks_config("repo-a", {"image": "python:3.12-slim"})


def test_parse_checks_config_requires_image_or_build():
    with pytest.raises(ConfigError):
        parse_checks_config("repo-a", {"command": "pytest -q"})


def test_parse_checks_config_rejects_both_image_and_build():
    with pytest.raises(ConfigError):
        parse_checks_config(
            "repo-a", {"image": "python:3.12-slim", "build": "Dockerfile", "command": "pytest"}
        )


def test_parse_checks_configs_maps_by_repo():
    configs = parse_checks_configs(
        {"checks": {"repo-a": {"image": "python:3.12-slim", "command": "pytest -q"}}}
    )
    assert set(configs) == {"repo-a"}
    assert configs["repo-a"].command == "pytest -q"


def test_checks_script_image_mode_runs_command_in_workdir():
    script = _checks_script(
        ChecksSpec(
            repo="r",
            execution_id="e",
            checkout_dir="/w",
            command="pytest -q",
            image="python:3.12-slim",
            working_dir="/workspace",
        )
    )
    assert "img=python:3.12-slim" in script
    assert "buildah bud" not in script
    assert "cd /workspace &&" in script
    assert "base64 -d | sh" in script
    assert "GIT_CONFIG_KEY_0=safe.directory" in script
    assert "-w /workspace" not in script


def test_checks_script_build_mode_builds_dockerfile():
    script = _checks_script(
        ChecksSpec(
            repo="r",
            execution_id="e",
            checkout_dir="/w",
            command="make test",
            build="Dockerfile.test",
        )
    )
    assert "buildah bud -f /workspace/Dockerfile.test" in script


def test_checks_pod_name_is_sanitised_and_bounded():
    name = checks_pod_name("Owner/Repo", "intake-1")
    assert name.startswith("project-checks-")
    assert name == name.lower()
    assert len(name) <= 63


class _FakeOps:
    def __init__(self, exec_result):
        self._exec_result = exec_result
        self.created = []
        self.copied = []
        self.deleted = []

    async def create(self, manifest):
        self.created.append(manifest)

    async def wait_ready(self, pod, namespace):
        pass

    async def copy_in(self, pod, namespace, source_dir, dest_dir):
        self.copied.append((source_dir, dest_dir))

    async def exec_capture(self, pod, namespace, script):
        return self._exec_result

    async def delete(self, pod, namespace):
        self.deleted.append(pod)


async def test_run_checks_returns_exit_code_and_output():
    ops = _FakeOps((1, "pytest failed\n1 failed", ""))
    runner = ChecksRunner(ops, namespace="agent-sandbox")
    result = await runner.run_checks(
        ChecksSpec(repo="r", execution_id="e", checkout_dir="/w", command="pytest", image="img")
    )
    assert isinstance(result, ChecksResult)
    assert result.exit_code == 1
    assert "1 failed" in result.output
    assert ops.copied == [("/w", "/workspace")]
    assert ops.deleted, "pod must be torn down"


async def test_run_checks_tears_down_on_success():
    ops = _FakeOps((0, "13 passed", ""))
    runner = ChecksRunner(ops, namespace="agent-sandbox")
    result = await runner.run_checks(
        ChecksSpec(repo="r", execution_id="e", checkout_dir="/w", command="pytest", image="img")
    )
    assert result.exit_code == 0
    assert ops.deleted


def _spec(**overrides) -> ChecksSpec:
    base = {
        "repo": "r",
        "execution_id": "e",
        "checkout_dir": "/w",
        "command": "pytest -q",
        "image": "python:3.12-slim",
        "working_dir": "/workspace",
    }
    base.update(overrides)
    return ChecksSpec(**base)


def test_checks_spec_changed_paths_defaults_to_empty():
    assert _spec().changed_paths == ()
    assert _spec(changed_paths=("a.py",)).changed_paths == ("a.py",)


def test_checks_script_exports_changed_paths():
    script = _checks_script(_spec(changed_paths=("src/a.py", "tests/test_a.py")))
    assert "export CHANGED_PATHS=" in script
    assert "src/a.py" in script
    assert "tests/test_a.py" in script


def test_checks_script_omits_changed_paths_when_empty():
    assert "CHANGED_PATHS" not in _checks_script(_spec())


def test_checks_script_changed_paths_separator_is_newline():
    script = _checks_script(_spec(changed_paths=("src/a.py", "src/b.py")))
    assert "src/a.py\nsrc/b.py" in script


def test_checks_script_changed_paths_quoting_survives_a_space():
    import subprocess

    from services.orchestration.checks.runner import _changed_paths_statement

    paths = ("src/a file.py", "tests/b.py")
    statement = _changed_paths_statement(_spec(changed_paths=paths))
    read_back = subprocess.run(
        ["sh", "-c", f'{statement}\nprintf "%s" "$CHANGED_PATHS"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert read_back.split("\n") == list(paths)


def test_fix_exports_changed_paths_for_the_verify_step():
    spec = _spec(fix_command="ruff check --fix .", changed_paths=("src/a.py",))
    script = _fix_inner_script(spec)
    assert script.startswith("export CHANGED_PATHS=")
    assert script.index("export CHANGED_PATHS=") < script.index("ruff check --fix .")
    assert "CHANGED_PATHS" not in _fix_inner_script(_spec(fix_command="ruff check --fix ."))


def _fake_settings():
    from types import SimpleNamespace

    return SimpleNamespace(
        gate_engine_namespace="agent-sandbox",
        gate_engine_builder_image="builder",
        gate_engine_privileged=False,
    )


def _patch_checks_stack(monkeypatch, runner_cls, *, fix_command=""):
    import scripts.config as config_mod
    import services.orchestration.checks.config as checks_config_mod
    import services.orchestration.checks.runner as runner_mod
    import services.orchestration.gates.pod_runner as pod_mod

    monkeypatch.setattr(config_mod, "get_settings", _fake_settings)
    monkeypatch.setattr(
        checks_config_mod,
        "checks_for_repo",
        lambda repo, settings: ChecksConfig(
            repo=repo, image="python:3.12-slim", command="pytest -q", fix_command=fix_command
        ),
    )
    monkeypatch.setattr(runner_mod, "ChecksRunner", runner_cls)
    monkeypatch.setattr(pod_mod, "KubectlPodOps", lambda *a, **k: object())


async def test_run_checks_activity_forwards_changed_paths(tmp_path, monkeypatch):
    from services.orchestration.temporal_activities.checks import RunChecksRequest, run_checks

    captured = {}

    class _Runner:
        def __init__(self, *args, **kwargs):
            pass

        async def run_checks(self, spec):
            captured["spec"] = spec
            return ChecksResult(exit_code=0, output="ok")

    _patch_checks_stack(monkeypatch, _Runner)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n")

    result = await run_checks(
        RunChecksRequest(
            repo="r",
            execution_id="e",
            workspace_path=str(workspace),
            commit_sha="sha",
            changed_paths=("src/a.py", "src/b.py"),
        )
    )

    assert result.ran
    assert captured["spec"].changed_paths == ("src/a.py", "src/b.py")


async def test_repair_forwards_changed_paths_into_the_spec(tmp_path, monkeypatch):
    from services.orchestration.temporal_activities.repair import (
        ApplyCIRepairRequest,
        _project_autofix,
    )

    captured = {}

    class _Runner:
        def __init__(self, *args, **kwargs):
            pass

        async def run_fix(self, spec, staging):
            captured["spec"] = spec
            return ChecksFixResult(verified=False, exit_code=1, output="")

    _patch_checks_stack(monkeypatch, _Runner, fix_command="ruff check --fix .")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n")

    applied = await _project_autofix(
        ApplyCIRepairRequest(
            task_id="t",
            execution_id="e",
            repo="r",
            base="main",
            commit="sha",
            instruction="fix it",
            changed_paths=("src/a.py",),
        ),
        str(workspace),
    )

    assert applied is False
    assert captured["spec"].changed_paths == ("src/a.py",)
