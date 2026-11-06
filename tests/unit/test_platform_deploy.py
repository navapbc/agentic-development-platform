"""Ordering, slot choice, and failure handling of the platform deploy step."""

from __future__ import annotations

import pytest
from scripts import platform_deploy as deployer

TASK_DEFINITION = "arn:aws:ecs:us-east-1:111111111111:task-definition/{}"
PINNED_CHECK = "python -m scripts.pinned_executions {}"
SETTINGS = {
    "cluster": "arn:aws:ecs:us-east-1:111111111111:cluster/platform",
    "services": {
        "coordination": {
            "service": "platform-coordination",
            "task_definition": TASK_DEFINITION.format("coordination:3"),
            "count": 1,
        },
    },
    "versioned_services": {
        "workflow-worker": {
            "slots": {
                "blue": "platform-workflow-worker-blue",
                "green": "platform-workflow-worker-green",
            },
            "task_definition": TASK_DEFINITION.format("workflow:4"),
            "count": 2,
        },
    },
    "tasks": {
        name: {
            "task_definition": TASK_DEFINITION.format(f"{name}:1"),
            "container": name,
            "security_groups": [f"sg-{name}"],
        }
        for name in ("db-migrate", "schedule-bootstrap")
    },
    "task_subnets": ["subnet-mgmt"],
}
BUILDS = {
    "workflow:1": "0.3.0.0",
    "workflow:2": "0.3.0.1",
    "workflow:3": "0.3.0.2",
    "workflow:4": "0.3.0.2",
}
SLOT = "platform-workflow-worker-{}"


REVIEW_SLOT = "platform-review-worker-{}"


class _Waiter:
    def __init__(self, ecs: _Ecs, name: str) -> None:
        self._ecs, self._name = ecs, name

    def wait(self, **kwargs) -> None:
        self._ecs.calls.append(("wait", self._name, kwargs))


class _Ecs:
    def __init__(
        self,
        exit_codes: dict[str, int] | None = None,
        slots: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self.exit_codes = exit_codes or {}
        self._runs: dict[str, tuple[str, str]] = {}
        initial = slots or {"blue": ("workflow:4", 0), "green": ("workflow:4", 0)}
        self.services = {
            SLOT.format(slot): {"taskDefinition": TASK_DEFINITION.format(td), "desiredCount": n}
            for slot, (td, n) in initial.items()
        }

    def run_task(self, **request):
        override = request["overrides"]["containerOverrides"][0]
        label = " ".join(override.get("command", [])) or override["name"]
        self.calls.append(("run_task", label, request))
        arn = f"arn:aws:ecs:us-east-1:111111111111:task/platform/{len(self._runs)}"
        self._runs[arn] = (override["name"], label)
        return {"tasks": [{"taskArn": arn}], "failures": []}

    def get_waiter(self, name: str) -> _Waiter:
        return _Waiter(self, name)

    def describe_tasks(self, cluster: str, tasks: list[str]):
        container, label = self._runs[tasks[0]]
        return {
            "tasks": [
                {
                    "stoppedReason": "Essential container in task exited",
                    "containers": [
                        {"name": "volume-permissions", "exitCode": 0},
                        {"name": container, "exitCode": self.exit_codes.get(label, 0)},
                    ],
                }
            ]
        }

    def describe_services(self, cluster: str, services: list[str]):
        return {"services": [{"serviceName": name, **self.services[name]} for name in services]}

    def describe_task_definition(self, taskDefinition: str):
        build = BUILDS[taskDefinition.rsplit("/", 1)[1]]
        image = (
            f"111111111111.dkr.ecr.us-east-1.amazonaws.com/platform:{build}-abc123-arm64@sha256:00"
        )
        return {
            "taskDefinition": {
                "containerDefinitions": [
                    {"name": "volume-permissions", "image": "public.ecr.aws/busybox:1"},
                    {"name": "workflow-worker", "image": image},
                ]
            }
        }

    def update_service(self, **request) -> None:
        self.calls.append(("update_service", request["service"], request))
        if request["service"] in self.services:
            state = self.services[request["service"]]
            state["desiredCount"] = request["desiredCount"]
            state["taskDefinition"] = request.get("taskDefinition", state["taskDefinition"])


def _steps(ecs: _Ecs) -> list[str]:
    return [call[1] for call in ecs.calls]


def _counts(ecs: _Ecs) -> dict[str, tuple[str, int]]:
    return {
        name.rsplit("-", 1)[1]: (state["taskDefinition"].rsplit("/", 1)[1], state["desiredCount"])
        for name, state in ecs.services.items()
    }


def test_the_migration_runs_before_the_services_and_the_schedules_after() -> None:
    ecs = _Ecs()

    deployer.deploy(ecs, SETTINGS)

    assert _steps(ecs) == [
        "db-migrate",
        "tasks_stopped",
        "platform-coordination",
        "platform-workflow-worker-blue",
        "services_stable",
        "schedule-bootstrap",
        "tasks_stopped",
        "python -m scripts.prune_worker_versions",
        "tasks_stopped",
    ]
    update = ecs.calls[3][2]
    assert (update["taskDefinition"], update["desiredCount"], update["forceNewDeployment"]) == (
        SETTINGS["versioned_services"]["workflow-worker"]["task_definition"],
        2,
        True,
    )
    assert _counts(ecs) == {"blue": ("workflow:4", 2), "green": ("workflow:4", 0)}


def test_the_one_shot_tasks_run_in_the_management_tier_with_their_groups() -> None:
    ecs = _Ecs()

    deployer.run_task(ecs, SETTINGS, "db-migrate")

    assert ecs.calls[0][2]["networkConfiguration"]["awsvpcConfiguration"] == {
        "subnets": ["subnet-mgmt"],
        "securityGroups": ["sg-db-migrate"],
        "assignPublicIp": "DISABLED",
    }


def test_a_failed_migration_leaves_the_services_untouched() -> None:
    ecs = _Ecs(exit_codes={"db-migrate": 1})

    with pytest.raises(deployer.DeployError, match="db-migrate exited with 1"):
        deployer.deploy(ecs, SETTINGS)

    assert not any(call[0] == "update_service" for call in ecs.calls)


def test_a_failed_prune_is_reported() -> None:
    ecs = _Ecs(exit_codes={"python -m scripts.prune_worker_versions": 1})

    with pytest.raises(deployer.DeployError, match="prune_worker_versions exited with 1"):
        deployer.deploy(ecs, SETTINGS)


def test_a_root_without_the_services_names_the_settings() -> None:
    with pytest.raises(deployer.DeployError, match="container_platform_image"):
        deployer.deploy(_Ecs(), {})


def test_a_new_build_starts_in_the_idle_slot_and_the_unpinned_old_slot_stops() -> None:
    ecs = _Ecs(slots={"blue": ("workflow:2", 2), "green": ("workflow:2", 0)})

    deployer.deploy(ecs, SETTINGS)

    steps = _steps(ecs)
    assert steps.index("platform-workflow-worker-green") < steps.index("services_stable")
    assert steps.index("services_stable") < steps.index(PINNED_CHECK.format("0.3.0.1"))
    assert _counts(ecs) == {"blue": ("workflow:2", 0), "green": ("workflow:4", 2)}


def test_an_old_slot_with_pinned_executions_keeps_running() -> None:
    check = PINNED_CHECK.format("0.3.0.1")
    ecs = _Ecs(
        exit_codes={check: deployer.PINNED_EXIT_CODE},
        slots={"blue": ("workflow:2", 2), "green": ("workflow:2", 0)},
    )

    deployer.deploy(ecs, SETTINGS)

    assert _counts(ecs) == {"blue": ("workflow:2", 2), "green": ("workflow:4", 2)}
    assert _steps(ecs)[-2:] == ["python -m scripts.prune_worker_versions", "tasks_stopped"]


def test_a_later_deploy_stops_the_old_slot_once_its_executions_finish() -> None:
    ecs = _Ecs(slots={"blue": ("workflow:2", 2), "green": ("workflow:3", 2)})

    deployer.deploy(ecs, SETTINGS)

    assert _counts(ecs) == {"blue": ("workflow:2", 0), "green": ("workflow:4", 2)}


def test_a_new_revision_of_the_running_build_replaces_the_active_slot_in_place() -> None:
    ecs = _Ecs(slots={"blue": ("workflow:2", 0), "green": ("workflow:3", 2)})

    deployer.deploy(ecs, SETTINGS)

    assert _counts(ecs) == {"blue": ("workflow:2", 0), "green": ("workflow:4", 2)}
    assert not any(step.startswith("python -m scripts.pinned_executions") for step in _steps(ecs))


def test_a_third_build_with_both_slots_busy_stops_before_any_service_moves() -> None:
    ecs = _Ecs(slots={"blue": ("workflow:1", 2), "green": ("workflow:2", 2)})

    with pytest.raises(deployer.DeployError, match="both workflow-worker slots"):
        deployer.deploy(ecs, SETTINGS)

    assert not any(call[0] == "update_service" for call in ecs.calls)


def test_workers_on_the_same_old_build_share_one_pinned_check() -> None:
    settings = {
        **SETTINGS,
        "versioned_services": {
            **SETTINGS["versioned_services"],
            "review-worker": {
                "slots": {"blue": REVIEW_SLOT.format("blue"), "green": REVIEW_SLOT.format("green")},
                "task_definition": TASK_DEFINITION.format("workflow:4"),
                "count": 1,
            },
        },
    }
    ecs = _Ecs(slots={"blue": ("workflow:2", 2), "green": ("workflow:2", 0)})
    for slot, count in (("blue", 1), ("green", 0)):
        ecs.services[REVIEW_SLOT.format(slot)] = {
            "taskDefinition": TASK_DEFINITION.format("workflow:2"),
            "desiredCount": count,
        }

    deployer.deploy(ecs, settings)

    assert _steps(ecs).count(PINNED_CHECK.format("0.3.0.1")) == 1
    assert ecs.services[REVIEW_SLOT.format("blue")]["desiredCount"] == 0
