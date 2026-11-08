"""inv eks-deploy runs the eks-deployer task, and the task deploys from inside the VPC."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import eks_deploy, eks_deploy_task


def _pod(namespace: str, name: str, labels: dict, host: bool = False, phase: str = "Running"):
    return {
        "metadata": {"namespace": namespace, "name": name, "labels": labels},
        "spec": {"hostNetwork": host},
        "status": {"phase": phase},
    }


def _policy(namespace: str, selector: dict) -> dict:
    return {"metadata": {"namespace": namespace}, "spec": {"podSelector": selector}}


def test_unselected_pods_skip_host_network_and_finished_pods() -> None:
    pods = [
        _pod("kube-system", "coredns", {"k8s-app": "kube-dns"}),
        _pod("kube-system", "aws-node", {"k8s-app": "aws-node"}, host=True),
        _pod("kube-system", "efs", {"app": "efs-csi-controller"}),
        _pod("agent-sandbox", "gate", {"app": "gate-engine"}),
        _pod("agent-sandbox", "done", {"app": "x"}, phase="Succeeded"),
        _pod("default", "stray", {"app": "x"}),
    ]
    policies = [
        _policy("kube-system", {"matchLabels": {"k8s-app": "kube-dns"}}),
        _policy(
            "agent-sandbox",
            {"matchExpressions": [{"key": "app", "operator": "In", "values": ["gate-engine"]}]},
        ),
    ]

    assert eks_deploy_task.unselected_pods(pods, policies) == [
        "kube-system/efs",
        "default/stray",
    ]


def test_an_empty_selector_selects_every_pod_in_its_namespace() -> None:
    pods = [_pod("aws-secrets-manager", "provider", {"app": "provider"})]

    assert eks_deploy_task.unselected_pods(pods, [_policy("aws-secrets-manager", {})]) == []


@pytest.mark.parametrize(
    ("operator", "values", "labels", "matches"),
    [
        ("NotIn", ["a"], {"k": "b"}, True),
        ("NotIn", ["a"], {"k": "a"}, False),
        ("Exists", [], {"k": "a"}, True),
        ("DoesNotExist", [], {"k": "a"}, False),
    ],
)
def test_selector_expressions(operator, values, labels, matches) -> None:
    selector = {"matchExpressions": [{"key": "k", "operator": operator, "values": values}]}

    assert eks_deploy_task._matches(selector, labels) is matches


def test_the_kubeconfig_mints_a_token_from_the_task_role(tmp_path) -> None:
    cluster = {
        "name": "agentic-platform-eks",
        "endpoint": "https://example.gr7.us-east-1.eks.amazonaws.com",
        "certificate_authority": "Q0E=",
        "region": "us-east-1",
    }
    eks_deploy_task.write_kubeconfig(cluster, tmp_path / "kubeconfig")

    config = json.loads((tmp_path / "kubeconfig").read_text())
    user = config["users"][0]["user"]["exec"]
    assert config["clusters"][0]["cluster"]["server"] == cluster["endpoint"]
    assert user["command"] == eks_deploy_task.AUTHENTICATOR
    assert user["args"] == ["token", "-i", "agentic-platform-eks"]
    assert "token" not in config["users"][0]["user"]


def test_restart_rolls_each_named_deployment(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(eks_deploy_task, "run", lambda args, **_: calls.append(args) or "")

    eks_deploy_task.restart(["activity-worker", "mcp-gateway"])

    assert [c[3:5] for c in calls[:2]] == [
        ["rollout", "restart"],
        ["rollout", "restart"],
    ]
    assert calls[0][5] == "deployment/activity-worker"
    assert [c[5] for c in calls[2:]] == ["deployment/activity-worker", "deployment/mcp-gateway"]


def test_the_mounted_secrets_cover_every_provider_object() -> None:
    keys = eks_deploy.mounted_secret_keys()

    assert "database_platform_app" in keys
    assert "temporal_ca" in keys
    assert "mcp_gateway_token" in keys


class _FakeSecrets:
    def __init__(self, staged: dict[str, bool]) -> None:
        self._staged = staged

    def describe_secret(self, SecretId):  # noqa: N803 - the boto3 parameter name
        if self._staged[SecretId]:
            return {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}}
        return {}


def test_secrets_without_a_current_value_are_left_out() -> None:
    arns = {"github_token": "arn:gh", "mcp_gateway_token": "arn:mcp"}
    secrets = _FakeSecrets({"arn:gh": False, "arn:mcp": True})

    assert eks_deploy.unset_secrets(list(arns), arns, secrets) == ["github_token"]


def test_a_secret_without_an_arn_is_an_error() -> None:
    with pytest.raises(eks_deploy.DeployError, match="no secret for github_token"):
        eks_deploy.unset_secrets(["github_token"], {}, _FakeSecrets({}))


def test_a_root_without_the_deployer_skips_the_deploy(capsys) -> None:
    assert eks_deploy.deploy(session=None, eks=None, command=["deploy"]) == 0
    assert "skipping" in capsys.readouterr().out


class _Waiter:
    def wait(self, **_):
        return None


class _FakeEcs:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.started: dict = {}

    def run_task(self, **request):
        self.started = request
        return {"tasks": [{"taskArn": "arn:aws:ecs:us-east-1:1:task/cluster/abc123"}]}

    def get_waiter(self, name):
        assert name == "tasks_stopped"
        return _Waiter()

    def describe_tasks(self, cluster, tasks):
        return {"tasks": [{"containers": [{"name": "eks-deployer", "exitCode": self.exit_code}]}]}


class _FakeLogs:
    class exceptions:
        ResourceNotFoundException = KeyError

    def __init__(self) -> None:
        self.streams: list[str] = []

    def get_paginator(self, name):
        logs = self

        class _Pages:
            def paginate(self, logGroupName, logStreamNames):  # noqa: N803
                logs.streams += logStreamNames
                yield {"events": [{"message": "[eks-deployer] deployment/litellm is rolled out"}]}

        return _Pages()


DEPLOYER = {
    "cluster": "arn:aws:ecs:us-east-1:1:cluster/platform",
    "task_subnets": ["subnet-m"],
    "log_group": "/aws/ecs/agentic-platform-eks-deployer",
    "tasks": {
        "eks-deployer": {
            "task_definition": "arn:td",
            "container": "eks-deployer",
            "security_groups": ["sg-d"],
        }
    },
}


def test_the_deployer_runs_in_the_management_tier_and_prints_its_log(capsys) -> None:
    ecs, logs = _FakeEcs(0), _FakeLogs()

    eks_deploy.run_deployer(ecs, logs, DEPLOYER, ["deploy", "--omit-secret", "github_token"])

    network = ecs.started["networkConfiguration"]["awsvpcConfiguration"]
    assert network == {
        "subnets": ["subnet-m"],
        "securityGroups": ["sg-d"],
        "assignPublicIp": "DISABLED",
    }
    override = ecs.started["overrides"]["containerOverrides"][0]
    assert override["command"] == ["deploy", "--omit-secret", "github_token"]
    assert logs.streams == ["eks-deployer/eks-deployer/abc123"]
    assert "rolled out" in capsys.readouterr().out


def test_a_failed_deployer_is_an_error() -> None:
    with pytest.raises(eks_deploy.DeployError, match="exited with 1"):
        eks_deploy.run_deployer(_FakeEcs(1), _FakeLogs(), DEPLOYER, ["deploy"])


def test_the_image_carries_everything_the_task_reads() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[2] / "docker/eks-deployer.Dockerfile"
    ).read_text()

    for path in ("scripts/eks_render.py", "scripts/eks_deploy_task.py", "deploy/kubernetes"):
        assert path in dockerfile
    assert "/opt/eks-deployer/charts/aws-load-balancer-controller.tgz" in dockerfile
    assert str(eks_deploy_task.CHART).endswith("charts/aws-load-balancer-controller.tgz")


def test_the_objects_kubectl_prints_one_after_another_are_all_read() -> None:
    text = (
        '{"kind": "Namespace", "metadata": {"name": "a"}}\n'
        '{\n  "kind": "List",\n  "items": [{"kind": "ConfigMap"}]\n}\n'
    )

    assert [o["kind"] for o in eks_deploy_task.json_objects(text)] == ["Namespace", "ConfigMap"]
