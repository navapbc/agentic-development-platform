"""Unit test for the invoke task surface."""

from __future__ import annotations

from pathlib import Path

import pytest
import tasks as tasks_module
from invoke.collection import Collection
from invoke.context import Context

EXPECTED_TASKS = {
    "tunnel-start",
    "tunnel-stop",
    "tunnel-status",
    "connectivity-check",
    "config-check",
    "local-up",
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
    "workflow-status",
    "durability-proof",
    "fetch-acli",
    "cbm-build",
    "publish-jumphost-image",
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
