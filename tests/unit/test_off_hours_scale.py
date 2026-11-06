"""The off-hours script the SSM Automation documents run."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra/terraform/off-hours/functions/scale.py"
PARAMETER = "/platform/off-hours/wake-counts"
ARN = "arn:aws:ecs:us-east-1:111111111111:service/platform/{}"


@pytest.fixture(scope="module")
def scale():
    spec = importlib.util.spec_from_file_location("off_hours_scale", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ecs:
    def __init__(self, counts: dict[str, int], stopping: dict[str, str] | None = None) -> None:
        self.services = {ARN.format(name): count for name, count in counts.items()}
        self.updates: list[tuple[str, int]] = []
        self.stopping = stopping or {}

    def describe_services(self, cluster: str, services: list[str]):
        assert cluster == "platform" and len(services) <= 10
        return {
            "services": [
                {
                    "serviceArn": arn,
                    "serviceName": arn.rsplit("/", 1)[1],
                    "desiredCount": self.services[arn],
                    "runningCount": self.services[arn],
                    "pendingCount": 0,
                }
                for arn in services
            ]
        }

    def list_tasks(self, cluster: str, serviceName: str, desiredStatus: str):
        assert desiredStatus == "STOPPED"
        return {"taskArns": [serviceName] if serviceName in self.stopping else []}

    def describe_tasks(self, cluster: str, tasks: list[str]):
        return {"tasks": [{"lastStatus": self.stopping[task]} for task in tasks]}

    def update_service(self, cluster: str, service: str, desiredCount: int) -> None:
        self.updates.append((service.rsplit("/", 1)[1], desiredCount))
        self.services[service] = desiredCount

    def counts(self) -> dict[str, int]:
        return {arn.rsplit("/", 1)[1]: count for arn, count in self.services.items()}


class _Ssm:
    def __init__(self, value: str = "{}") -> None:
        self.value = value

    def get_parameter(self, Name: str):
        assert Name == PARAMETER
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, Name: str, Value: str, Overwrite: bool) -> None:
        self.value = Value

    def recorded(self) -> dict[str, int]:
        return {arn.rsplit("/", 1)[1]: count for arn, count in json.loads(self.value).items()}


def _events(*names: str) -> dict:
    return {
        "Services": [ARN.format(name) for name in names],
        "Parameter": PARAMETER,
        "WaitSeconds": 0,
    }


def test_sleep_records_the_running_services_and_scales_them_to_zero(scale) -> None:
    ecs, ssm = (
        _Ecs({"workflow-worker-blue": 2, "workflow-worker-green": 0, "coordination": 1}),
        _Ssm(),
    )

    result = scale.sleep(
        _events("workflow-worker-blue", "workflow-worker-green", "coordination"), None, ecs, ssm
    )

    assert ssm.recorded() == {"workflow-worker-blue": 2, "coordination": 1}
    assert set(ecs.counts().values()) == {0}
    assert result["still_running"] == []


def test_wake_restores_only_the_recorded_services(scale) -> None:
    ecs = _Ecs({"workflow-worker-blue": 0, "workflow-worker-green": 0, "coordination": 0})
    ssm = _Ssm()
    ssm.value = json.dumps({ARN.format("workflow-worker-blue"): 2, ARN.format("coordination"): 1})

    scale.wake(
        _events("workflow-worker-blue", "workflow-worker-green", "coordination"), None, ecs, ssm
    )

    assert ecs.counts() == {
        "workflow-worker-blue": 2,
        "workflow-worker-green": 0,
        "coordination": 1,
    }
    assert ssm.recorded() == {}


def test_a_second_sleep_keeps_the_counts_the_first_recorded(scale) -> None:
    ecs, ssm = _Ecs({"coordination": 1}), _Ssm()
    scale.sleep(_events("coordination"), None, ecs, ssm)

    scale.sleep(_events("coordination"), None, ecs, ssm)

    assert ssm.recorded() == {"coordination": 1}


def test_each_wake_step_restores_and_forgets_only_its_own_services(scale) -> None:
    ecs = _Ecs({"temporal-server": 1, "clickhouse": 1, "coordination": 1})
    ssm = _Ssm()
    scale.sleep(_events("coordination"), None, ecs, ssm)
    scale.sleep(_events("temporal-server", "clickhouse"), None, ecs, ssm)

    scale.wake(_events("temporal-server", "clickhouse"), None, ecs, ssm)

    assert ecs.counts() == {"temporal-server": 1, "clickhouse": 1, "coordination": 0}
    assert ssm.recorded() == {"coordination": 1}


def test_describe_batches_eleven_services_in_two_calls(scale) -> None:
    names = [f"service-{index}" for index in range(11)]
    ecs, ssm = _Ecs(dict.fromkeys(names, 1)), _Ssm()

    scale.sleep(_events(*names), None, ecs, ssm)

    assert len(ssm.recorded()) == 11


def test_a_task_still_running_its_containers_keeps_the_service_pending(scale) -> None:
    ecs = _Ecs(
        {"coordination": 1, "workflow-worker-blue": 1},
        stopping={"coordination": "RUNNING", "workflow-worker-blue": "DEPROVISIONING"},
    )

    result = scale.sleep(_events("coordination", "workflow-worker-blue"), None, ecs, _Ssm())

    assert result["still_running"] == [ARN.format("coordination")]
