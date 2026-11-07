"""Unit test for the invoke task surface."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import tasks as tasks_module
from invoke.collection import Collection
from invoke.context import Context
from invoke.exceptions import Exit
from invoke.runners import Result
from tests.support import hermetic_settings

EXPECTED_TASKS = {
    "tunnel-start",
    "tunnel-stop",
    "tunnel-status",
    "connectivity-check",
    "config-check",
    "repos-compose",
    "local-up",
    "temporal-upgrade",
    "local-down",
    "local-stop",
    "local-destroy",
    "logs",
    "db-migrate",
    "schedules-install",
    "db-reset",
    "db-bootstrap-roles",
    "seed",
    "import-rebar-tickets",
    "rebar-changes",
    "rebar-store-remote",
    "rebar-review-plan",
    "rebar-review-code",
    "operator-done",
    "smoke-test",
    "prompts-seed",
    "evidence-bucket-bootstrap",
    "review-eval",
    "context-eval",
    "cost-rollup",
    "reconcile-usage",
    "test",
    "ci",
    "sast",
    "terraform-test",
    "workflow-status",
    "durability-proof",
    "prune-worker-versions",
    "fetch-acli",
    "cbm-build",
    "publish-jumphost-image",
    "publish-temporal-images",
    "publish-platform-images",
    "go-test",
    "temporal-certs",
    "clickhouse-certs",
    "langfuse-ui",
    "temporal-deploy",
    "platform-deploy",
    "temporal-ui",
    "reap",
    "scrub-workspaces",
    "intake",
    "index-repo",
    "repair",
    "gate",
    "restart",
    "demo-durable-task",
    "demo-durable-counter",
    "demo-coordination",
    "demo-repo-intelligence",
    "demo-draft-pr",
    "approve-pr",
    "k8s-deploy",
    "k8s-delete",
    "webhook-setup",
    "webhook-teardown",
}


def test_invoke_namespace_exposes_documented_tasks():
    ns = tasks_module.ns
    assert isinstance(ns, Collection)
    assert set(ns.task_names) == EXPECTED_TASKS


class _RecordingContext(Context):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []

    def run(self, command: str, **_: object) -> None:
        self.commands.append(command)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    return tmp_path


def _in_activity_worker(command: str) -> str:
    return f"{tasks_module.COMPOSE} -f compose.yaml exec -T activity-worker {command}"


def test_repair_runs_in_the_activity_worker(project):
    container, host = _RecordingContext(), _RecordingContext()

    tasks_module.repair(container, "T-1", retry=True)
    tasks_module.repair(host, "T-1", abandon=True, local=True)

    assert container.commands == [_in_activity_worker("python -m scripts.repair T-1 --retry")]
    assert host.commands == [f"{tasks_module.HOST_PYTHON} -m scripts.repair T-1 --abandon"]


def test_gate_runs_in_the_activity_worker(project):
    container, host = _RecordingContext(), _RecordingContext()

    tasks_module.gate(container, "T-1", "plan", approve=True)
    tasks_module.gate(host, "T-1", "plan", reject=True, local=True)

    assert container.commands == [
        _in_activity_worker("python -m scripts.gate_decision T-1 --gate plan --approve")
    ]
    assert host.commands == [
        f"{tasks_module.HOST_PYTHON} -m scripts.gate_decision T-1 --gate plan --reject"
    ]


def test_restart_runs_in_the_activity_worker(project):
    container, host = _RecordingContext(), _RecordingContext()

    tasks_module.restart(container, "T-1", repo="demo")
    tasks_module.restart(host, "T-1", local=True)

    assert container.commands == [_in_activity_worker("python -m scripts.restart T-1 --repo demo")]
    assert host.commands == [f"{tasks_module.HOST_PYTHON} -m scripts.restart T-1"]


def test_host_tasks_run_the_interpreter_that_runs_invoke(project):
    """A bare `python` on the host has none of the project's dependencies, so it fails on import."""
    host = _RecordingContext()

    tasks_module.smoke_test(host)
    tasks_module.config_check(host)
    tasks_module.connectivity_check(host)

    assert host.commands == [
        f"{tasks_module.HOST_PYTHON} -m scripts.smoke_test",
        f"{tasks_module.HOST_PYTHON} -m scripts.config_check",
        f"{tasks_module.HOST_PYTHON} -m scripts.connectivity",
    ]
    assert tasks_module.HOST_PYTHON != "python"


def test_no_host_task_invokes_a_bare_python(project):
    source = Path(tasks_module.__file__).read_text(encoding="utf-8")

    assert 'c.run("python ' not in source
    assert 'c.run(f"python ' not in source


def test_rebar_store_remote_runs_in_a_one_off_worker_container(project):
    container, host = _RecordingContext(), _RecordingContext()

    tasks_module.rebar_store_remote(container)
    tasks_module.rebar_store_remote(host, local=True)

    assert container.commands == [
        f"{tasks_module.COMPOSE} -f compose.yaml run --rm --no-deps activity-worker "
        "python -m scripts.rebar_store_remote"
    ]
    assert host.commands == [f"{tasks_module.HOST_PYTHON} -m scripts.rebar_store_remote"]


def test_temporal_certs_passes_each_name_and_switch_to_the_script(project):
    context = _RecordingContext()

    tasks_module.temporal_certs(
        context,
        "generate",
        out="~/tls",
        frontend_name="a.internal, b.internal",
        internal_name="coordination.platform.internal",
        no_frontend=True,
        force=True,
    )

    assert context.commands == [
        f"{tasks_module.HOST_PYTHON} -m scripts.temporal_certs generate --out '~/tls' --days 397"
        " --frontend-name a.internal --frontend-name b.internal"
        " --internal-name coordination.platform.internal --no-frontend --force"
    ]


def test_temporal_certs_put_signs_with_the_configured_profile(project, monkeypatch):
    recorded: list[tuple[str, dict]] = []

    class _EnvContext(Context):
        def run(self, command: str, **kwargs: object) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
            recorded.append((command, kwargs["env"]))

    monkeypatch.setenv("AWS_PROFILE", "sandbox")
    from scripts.config import get_settings

    get_settings.cache_clear()
    tasks_module.temporal_certs(_EnvContext(), "put", dir="~/tls", import_to_acm=True)
    get_settings.cache_clear()

    [(command, env)] = recorded
    assert command.endswith("-m scripts.temporal_certs put --dir '~/tls' --import-to-acm")
    assert env["AWS_PROFILE"] == "sandbox"


@pytest.mark.parametrize(
    ("action", "options", "message"),
    [
        ("generate", {}, "provide --out"),
        ("put", {}, "provide --dir"),
        ("rotate", {}, "generate or put"),
    ],
)
def test_temporal_certs_refuses_an_incomplete_request(project, action, options, message):
    from invoke import Exit

    with pytest.raises(Exit, match=message):
        tasks_module.temporal_certs(_RecordingContext(), action, **options)


class _TerraformContext(Context):
    def __init__(self, version: str, *, exited: int = 0, stderr: str = "") -> None:
        super().__init__()
        self.version = version
        self.exited = exited
        self.stderr = stderr
        self.commands: list[str] = []

    def run(self, command: str, **_: object) -> Result:
        self.commands.append(command)
        return Result(
            stdout=json.dumps({"terraform_version": self.version}),
            stderr=self.stderr,
            exited=self.exited,
        )


def _pinned_terraform_version() -> str:
    path = Path(tasks_module.__file__).parent / ".terraform-version"
    return path.read_text(encoding="utf-8").strip()


def test_terraform_modules_finds_a_composition_root_below_the_top_level(tmp_path):
    (tmp_path / "ecs-cluster" / "tests").mkdir(parents=True)
    (tmp_path / "examples" / "private-bedrock" / "tests").mkdir(parents=True)
    (tmp_path / "ecs-cluster" / ".terraform" / "modules" / "vendored" / "tests").mkdir(parents=True)

    assert tasks_module._terraform_modules(str(tmp_path)) == [
        str(tmp_path / "ecs-cluster"),
        str(tmp_path / "examples" / "private-bedrock"),
    ]


def test_terraform_test_refuses_a_version_other_than_the_pin(monkeypatch):
    monkeypatch.delenv("TFENV_TERRAFORM_VERSION", raising=False)
    context = _TerraformContext("0.0.0")

    with pytest.raises(Exit, match=_pinned_terraform_version()):
        tasks_module.terraform_test(context)

    assert context.commands == ["terraform version -json"]


def test_terraform_test_reports_a_terraform_it_could_not_run():
    context = _TerraformContext("", exited=127, stderr="bash: terraform: command not found")

    with pytest.raises(Exit, match="command not found"):
        tasks_module.terraform_test(context)

    assert context.commands == ["terraform version -json"]


def test_terraform_test_names_the_tfenv_override_that_selects_the_wrong_version(monkeypatch):
    monkeypatch.setenv("TFENV_TERRAFORM_VERSION", "1.14.3")

    with pytest.raises(Exit, match="Unset TFENV_TERRAFORM_VERSION, which selects 1.14.3"):
        tasks_module.terraform_test(_TerraformContext("1.14.3"))


def test_terraform_test_points_past_a_shadowing_binary_when_no_override_is_set(monkeypatch):
    monkeypatch.delenv("TFENV_TERRAFORM_VERSION", raising=False)

    with pytest.raises(Exit, match="ahead of any other terraform on PATH"):
        tasks_module.terraform_test(_TerraformContext("0.0.0"))


def test_ci_installs_the_pinned_terraform_version():
    workflow = Path(tasks_module.__file__).parent / ".github" / "workflows" / "ci.yml"

    assert f'terraform_version: "{_pinned_terraform_version()}"' in workflow.read_text(
        encoding="utf-8"
    )


def test_terraform_test_runs_every_module_on_the_pinned_version():
    context = _TerraformContext(_pinned_terraform_version())

    tasks_module.terraform_test(context)

    modules = tasks_module._terraform_modules(
        str(Path(tasks_module.__file__).parent / "infra" / "terraform")
    )
    assert context.commands[0] == "terraform version -json"
    assert context.commands[1:] == [
        command
        for module in modules
        for command in (
            f"terraform -chdir={module} init -backend=false -input=false",
            f"terraform -chdir={module} test",
        )
    ]


def test_go_modules_finds_each_module_and_skips_vendored_copies(tmp_path):
    for module in ("healthprobe", "tls-sidecar", "tls-sidecar/vendor/example.com/lib"):
        (tmp_path / module).mkdir(parents=True)
        (tmp_path / module / "go.mod").write_text("module example\n", encoding="utf-8")

    assert tasks_module._go_modules(str(tmp_path)) == [
        str(tmp_path / "healthprobe"),
        str(tmp_path / "tls-sidecar"),
    ]


def test_ci_tests_go_with_the_version_the_images_build_with():
    root = Path(tasks_module.__file__).parent
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    pinned = re.search(r'go-version: "(\d+\.\d+)\.\d+"', workflow).group(1)
    builders = {
        match.group(1)
        for dockerfile in (root / "docker").glob("*Dockerfile")
        for match in re.finditer(r"/golang:(\d+\.\d+)-", dockerfile.read_text(encoding="utf-8"))
    }

    assert builders == {pinned}


class _GoContext(Context):
    def __init__(self, *, unformatted: str = "") -> None:
        super().__init__()
        self.unformatted = unformatted
        self.commands: list[str] = []

    def run(self, command: str, **_: object) -> Result:
        self.commands.append(command)
        return Result(stdout=self.unformatted if command.startswith("gofmt") else "go version")


def test_go_test_vets_and_tests_every_module():
    context = _GoContext()

    tasks_module.go_test(context)

    modules = tasks_module._go_modules(str(Path(tasks_module.__file__).parent / "docker"))
    assert modules and context.commands[0] == "go version"
    assert [command for command in context.commands if "go test" in command] == [
        f"cd {module} && go vet ./... && go test ./..." for module in modules
    ]


def test_go_test_refuses_unformatted_code():
    with pytest.raises(Exit, match="gofmt would rewrite"):
        tasks_module.go_test(_GoContext(unformatted="docker/tls-sidecar/main.go"))


def test_operator_done_reads_the_workspace_artifacts_at_their_host_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "scripts.config.get_settings",
        lambda: hermetic_settings(source_control_local_artifacts_root="/workspaces/.agent-publish"),
    )

    assert tasks_module._host_source_control_env() == {
        "SOURCE_CONTROL_LOCAL_ARTIFACTS_ROOT": str(
            tmp_path / ".agent-workspaces" / ".agent-publish"
        )
    }
