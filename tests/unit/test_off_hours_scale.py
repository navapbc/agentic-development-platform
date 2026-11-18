"""The off-hours script the SSM Automation documents run."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SCRIPT = Path(__file__).resolve().parents[2] / "infra/terraform/off-hours/functions/scale.py"
PARAMETER = "/platform/off-hours/wake-counts"
ARN = "arn:aws:ecs:us-east-1:111111111111:service/platform/{}"


@pytest.fixture(scope="module")
def scale():
    spec = importlib.util.spec_from_file_location("off_hours_scale", SCRIPT)
    assert spec is not None and spec.loader is not None
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


DEPLOYER = {
    "Cluster": "arn:aws:ecs:us-east-1:111111111111:cluster/platform",
    "TaskFamily": "platform-eks-deployer",
    "Container": "eks-deployer",
    "Subnets": ["subnet-management"],
    "SecurityGroups": ["sg-deployer"],
    "LogGroup": "/aws/ecs/platform-eks-deployer",
}
DATABASE = {
    "ClusterArn": "arn:aws:rds:us-east-1:111111111111:cluster:platform",
    "SecretArn": "arn:aws:secretsmanager:us-east-1:111111111111:secret:platform/database/monitor",
}


class _Deployer(_Ecs):
    """ECS that also runs the eks-deployer task, which exits with `exit_code` at once."""

    def __init__(self, counts: dict[str, int], exit_code: int = 0) -> None:
        super().__init__(counts)
        self.exit_code = exit_code
        self.commands: list[list[str]] = []

    def run_task(self, **kwargs):
        assert kwargs["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "DISABLED"
        self.commands.append(kwargs["overrides"]["containerOverrides"][0]["command"])
        return {"tasks": [{"taskArn": "arn:aws:ecs:us-east-1:111111111111:task/platform/t1"}]}

    def describe_tasks(self, cluster: str, tasks: list[str]):
        if tasks == ["arn:aws:ecs:us-east-1:111111111111:task/platform/t1"]:
            container = {"name": "eks-deployer", "exitCode": self.exit_code}
            return {"tasks": [{"lastStatus": "STOPPED", "containers": [container]}]}
        return super().describe_tasks(cluster, tasks)


class _Logs:
    class exceptions:  # noqa: N801
        ResourceNotFoundException = LookupError

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def get_log_events(self, logGroupName: str, logStreamName: str, startFromHead: bool, **page):
        assert logStreamName == "eks-deployer/eks-deployer/t1"
        events = [] if page.get("nextToken") else [{"message": line} for line in self.lines]
        return {"events": events, "nextForwardToken": "end"}


class _RdsData:
    def __init__(self, rows: list[dict] | None = None, resuming: bool = False) -> None:
        self.rows = rows or []
        self.resuming = resuming

    def execute_statement(self, **kwargs):
        assert kwargs["secretArn"] == DATABASE["SecretArn"]
        if self.resuming:
            raise ClientError({"Error": {"Code": "DatabaseResumingException"}}, "ExecuteStatement")
        return {"formattedRecords": json.dumps(self.rows)}


class _Sns:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def publish(self, TopicArn: str, Subject: str, Message: str) -> None:
        self.messages.append(Message)


class _CloudWatch:
    def __init__(self) -> None:
        self.metrics: list[tuple[str, float]] = []

    def put_metric_data(self, Namespace: str, MetricData: list[dict]) -> None:
        self.metrics.extend((datum["MetricName"], datum["Value"]) for datum in MetricData)


def _check_events(*names: str, allowed: list[str] | None = None) -> dict:
    return {
        "Check": "BeforePause",
        "Document": "sleep",
        "Namespace": "platform/OffHours",
        "Topic": "arn:aws:sns:us-east-1:111111111111:platform-off-hours-alerts",
        "Services": [ARN.format(name) for name in names],
        "Deployer": DEPLOYER,
        "Deployments": ["tenant-workers", "litellm"],
        "DeployerWaitSeconds": 0,
        "Database": {**DATABASE, "AllowedUsers": allowed or []},
        "WaitSeconds": 0,
    }


def _connection(pid: int, user: str) -> dict:
    return {"pid": pid, "usename": user, "datname": user, "client_addr": "10.0.1.5"}


def test_the_check_fails_and_alerts_naming_each_service_pod_and_connection(scale) -> None:
    ecs = _Deployer({"coordination": 1, "workflow-worker-blue": 0}, exit_code=1)
    logs = _Logs([f"{scale.RUNNING_POD}tenant-adp/activity-worker-5f8-xyz"])
    sns, cloudwatch = _Sns(), _CloudWatch()
    rds_data = _RdsData([_connection(41, "temporal"), _connection(42, "platform_app")])

    with pytest.raises(RuntimeError, match="BeforePause found 4"):
        scale.check(
            _check_events("coordination", "workflow-worker-blue"),
            None,
            ecs,
            logs,
            rds_data,
            sns,
            cloudwatch,
        )

    assert ecs.commands == [["check-stopped", "tenant-workers", "litellm"]]
    assert cloudwatch.metrics == [("CheckFindings", 4)]
    message = sns.messages[0]
    for finding in (
        "ECS service coordination runs a task",
        "EKS pod tenant-adp/activity-worker-5f8-xyz is running",
        "Database connection 41 of temporal",
        "Database connection 42 of platform_app",
    ):
        assert finding in message
    assert "workflow-worker-blue" not in message


def test_the_check_before_the_foundation_allows_the_foundation_users(scale) -> None:
    sns, cloudwatch = _Sns(), _CloudWatch()

    result = scale.check(
        _check_events("coordination", allowed=["temporal"]),
        None,
        _Deployer({"coordination": 0}),
        _Logs([]),
        _RdsData([_connection(41, "temporal")]),
        sns,
        cloudwatch,
    )

    assert result == {"findings": []}
    assert sns.messages == []
    assert cloudwatch.metrics == [("CheckFindings", 0)]


def test_a_resuming_cluster_holds_no_connection(scale) -> None:
    result = scale.check(
        _check_events("coordination"),
        None,
        _Deployer({"coordination": 0}),
        _Logs([]),
        _RdsData(resuming=True),
        _Sns(),
        _CloudWatch(),
    )

    assert result == {"findings": []}


def test_scale_deployments_fails_when_the_deployer_fails(scale) -> None:
    events = {
        "Command": "scale-up",
        "Deployments": ["tenant-workers"],
        "Deployer": DEPLOYER,
        "WaitSeconds": 0,
    }

    with pytest.raises(RuntimeError, match="scale-up exited with 1"):
        scale.scale_deployments(events, None, _Deployer({}, exit_code=1), _Logs([]))


class _Healthy(_Ecs):
    def __init__(self, health: str) -> None:
        super().__init__({"temporal-server": 1})
        self.health = health

    def list_tasks(self, cluster: str, serviceName: str, desiredStatus: str):
        return {"taskArns": [serviceName] if desiredStatus == "RUNNING" else []}

    def describe_tasks(self, cluster: str, tasks: list[str]):
        return {"tasks": [{"lastStatus": "RUNNING", "healthStatus": self.health}]}


def test_the_wake_stops_before_the_workers_while_temporal_is_unhealthy(scale) -> None:
    events = {"Services": [ARN.format("temporal-server")], "WaitSeconds": 0}

    with pytest.raises(RuntimeError, match="temporal-server did not report healthy"):
        scale.await_healthy(events, None, _Healthy("UNKNOWN"))
    assert scale.await_healthy(events, None, _Healthy("HEALTHY")) == {
        "healthy": [ARN.format("temporal-server")]
    }


def test_a_failed_run_is_counted_and_alerts_with_its_execution(scale) -> None:
    sns, cloudwatch = _Sns(), _CloudWatch()
    events = {
        "Result": "Failed",
        "Document": "wake",
        "Namespace": "platform/OffHours",
        "Topic": "arn:aws:sns:us-east-1:111111111111:platform-off-hours-alerts",
        "ExecutionId": "0123-abcd",
    }

    scale.record(events, None, cloudwatch, sns)

    assert cloudwatch.metrics == [("Failed", 1)]
    assert "0123-abcd" in sns.messages[0]


def test_the_function_dispatches_each_step_to_its_handler(scale, monkeypatch) -> None:
    calls = []
    monkeypatch.setitem(scale.HANDLERS, "wake", lambda events, context: calls.append(events) or 7)

    assert scale.handler({"Handler": "wake", "Input": {"Services": []}}, None) == 7
    assert calls == [{"Services": []}]
    with pytest.raises(ValueError, match="no off-hours handler named 'drop_database'"):
        scale.handler({"Handler": "drop_database", "Input": {}}, None)


INVOKE = SCRIPT.with_name("invoke.py")


class _Lambda:
    def __init__(
        self, payload: object, function_error: str | None = None, throttles: int = 0
    ) -> None:
        self.payload = payload
        self.function_error = function_error
        self.throttles = throttles
        self.requests: list[dict] = []

    def invoke(self, FunctionName: str, Payload: bytes):
        self.requests.append({"FunctionName": FunctionName, **json.loads(Payload)})
        if len(self.requests) <= self.throttles:
            raise ClientError(
                {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
                "Invoke",
            )
        response: dict[str, object] = {"Payload": io.BytesIO(json.dumps(self.payload).encode())}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


@pytest.fixture(scope="module")
def invoke():
    spec = importlib.util.spec_from_file_location("off_hours_invoke", INVOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _step(handler: str, throttle_seconds: int = 60) -> dict:
    return {
        "Function": "platform-off-hours",
        "Handler": handler,
        "Input": {"Check": "BeforePause"},
        "ReadTimeout": 590,
        "ThrottleSeconds": throttle_seconds,
    }


def test_a_step_returns_what_the_function_returns(invoke, monkeypatch) -> None:
    client = _Lambda({"findings": []})
    monkeypatch.setattr(invoke.boto3, "client", lambda *args, **kwargs: client)

    assert invoke.invoke(_step("check"), None) == {"findings": []}
    assert client.requests == [
        {
            "FunctionName": "platform-off-hours",
            "Handler": "check",
            "Input": {"Check": "BeforePause"},
        }
    ]


def test_a_step_fails_when_the_function_raises(invoke, monkeypatch) -> None:
    client = _Lambda(
        {"errorMessage": "BeforePause found 1. EKS pod litellm-0 is running."}, "Unhandled"
    )
    monkeypatch.setattr(invoke.boto3, "client", lambda *args, **kwargs: client)

    with pytest.raises(RuntimeError, match="check failed. BeforePause found 1"):
        invoke.invoke(_step("check"), None)


def test_a_throttled_step_retries_until_the_function_accepts_it(invoke, monkeypatch) -> None:
    client = _Lambda({"result": "Failed"}, throttles=2)
    monkeypatch.setattr(invoke.boto3, "client", lambda *args, **kwargs: client)
    monkeypatch.setattr(invoke.time, "sleep", lambda seconds: None)

    assert invoke.invoke(_step("record"), None) == {"result": "Failed"}
    assert len(client.requests) == 3


def test_a_step_still_throttled_at_its_deadline_fails(invoke, monkeypatch) -> None:
    client = _Lambda({}, throttles=99)
    monkeypatch.setattr(invoke.boto3, "client", lambda *args, **kwargs: client)
    monkeypatch.setattr(invoke.time, "sleep", lambda seconds: None)

    with pytest.raises(ClientError, match="TooManyRequestsException"):
        invoke.invoke(_step("record", throttle_seconds=0), None)
    assert len(client.requests) == 1
