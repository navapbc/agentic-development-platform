"""Ordering and failure handling of the Temporal deploy step."""

from __future__ import annotations

import pytest
from scripts import temporal_deploy as deployer

SETTINGS = {
    "cluster": "arn:aws:ecs:us-east-1:111111111111:cluster/platform",
    "server_service": "platform-temporal-server",
    "server_task_definition": "arn:aws:ecs:us-east-1:111111111111:task-definition/server:7",
    "server_count": 2,
    "admin_tools_task_definition": "arn:aws:ecs:us-east-1:111111111111:task-definition/admin:3",
    "admin_tools_subnets": ["subnet-a"],
    "admin_tools_security_groups": ["sg-admin", "sg-client"],
}


class _Waiter:
    def __init__(self, ecs: _Ecs, name: str) -> None:
        self._ecs, self._name = ecs, name

    def wait(self, **kwargs) -> None:
        self._ecs.calls.append(("wait", self._name))


class _Ecs:
    def __init__(self, exit_codes: dict[str, int] | None = None, failures: list | None = None):
        self.calls: list[tuple] = []
        self.exit_codes = exit_codes or {}
        self.failures = failures or []
        self._actions: dict[str, str] = {}

    def run_task(self, **request):
        action = request["overrides"]["containerOverrides"][0]["command"][0]
        self.calls.append(("run_task", action, request))
        arn = f"arn:aws:ecs:us-east-1:111111111111:task/platform/{action}"
        self._actions[arn] = action
        return {"tasks": [{"taskArn": arn}], "failures": self.failures}

    def get_waiter(self, name: str) -> _Waiter:
        return _Waiter(self, name)

    def describe_tasks(self, cluster: str, tasks: list[str]):
        action = self._actions[tasks[0]]
        return {
            "tasks": [
                {
                    "stoppedReason": "Essential container in task exited",
                    "containers": [
                        {"name": "volume-permissions", "exitCode": 0},
                        {"name": "admin-tools", "exitCode": self.exit_codes.get(action, 0)},
                    ],
                }
            ]
        }

    def update_service(self, **request) -> None:
        self.calls.append(("update_service", request))


def _steps(ecs: _Ecs) -> list[str]:
    return [call[1] if call[0] in ("run_task", "wait") else call[0] for call in ecs.calls]


def test_the_schema_runs_before_the_server_moves_and_the_namespace_after() -> None:
    ecs = _Ecs()

    deployer.deploy(ecs, SETTINGS)

    assert _steps(ecs) == [
        "schema",
        "tasks_stopped",
        "update_service",
        "services_stable",
        "namespace",
        "tasks_stopped",
    ]
    update = next(call[1] for call in ecs.calls if call[0] == "update_service")
    assert update == {
        "cluster": SETTINGS["cluster"],
        "service": "platform-temporal-server",
        "taskDefinition": SETTINGS["server_task_definition"],
        "desiredCount": 2,
        "forceNewDeployment": True,
    }


def test_the_ui_restarts_after_the_namespace_to_load_the_current_ca() -> None:
    ecs = _Ecs()

    deployer.deploy(ecs, {**SETTINGS, "ui_service": "platform-temporal-ui"})

    assert _steps(ecs)[-3:] == ["tasks_stopped", "update_service", "services_stable"]
    restart = [call[1] for call in ecs.calls if call[0] == "update_service"][-1]
    assert restart == {
        "cluster": SETTINGS["cluster"],
        "service": "platform-temporal-ui",
        "forceNewDeployment": True,
    }


def test_the_admin_tools_task_runs_privately_with_the_client_group() -> None:
    ecs = _Ecs()

    deployer.run_admin_tools(ecs, SETTINGS, "schema")

    request = ecs.calls[0][2]
    assert request["networkConfiguration"]["awsvpcConfiguration"] == {
        "subnets": ["subnet-a"],
        "securityGroups": ["sg-admin", "sg-client"],
        "assignPublicIp": "DISABLED",
    }
    assert request["taskDefinition"] == SETTINGS["admin_tools_task_definition"]


def test_a_failed_schema_task_leaves_the_server_untouched() -> None:
    ecs = _Ecs(exit_codes={"schema": 1})

    with pytest.raises(deployer.DeployError, match="schema task exited with 1"):
        deployer.deploy(ecs, SETTINGS)

    assert not any(call[0] == "update_service" for call in ecs.calls)


def test_a_task_that_cannot_start_is_reported() -> None:
    ecs = _Ecs(failures=[{"reason": "RESOURCE:CPU"}])

    with pytest.raises(deployer.DeployError, match="did not start"):
        deployer.deploy(ecs, SETTINGS)


def test_a_control_plane_without_images_names_the_settings() -> None:
    with pytest.raises(deployer.DeployError, match="temporal_server_image"):
        deployer.deploy(_Ecs(), {})
