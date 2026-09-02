import pytest
from services.integration.credentials import ConfigError
from services.orchestration.checks.config import (
    ChecksConfig,
    parse_checks_config,
    parse_checks_configs,
)
from services.orchestration.checks.runner import (
    ChecksResult,
    ChecksRunner,
    ChecksSpec,
    _checks_script,
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
