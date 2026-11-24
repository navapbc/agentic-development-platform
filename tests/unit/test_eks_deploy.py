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

    eks_deploy_task.restart(["tenant-workers", "inbound-webhook"], {"tenants": [{"name": "adp"}]})

    assert [c[1:6] for c in calls[:2]] == [
        ["-n", "tenant-adp", "rollout", "restart", "deployment/activity-worker"],
        ["-n", "agentic-dev-platform", "rollout", "restart", "deployment/inbound-webhook"],
    ]
    assert [c[1:3] + c[5:6] for c in calls[2:]] == [
        ["-n", "tenant-adp", "deployment/activity-worker"],
        ["-n", "agentic-dev-platform", "deployment/inbound-webhook"],
    ]


def test_restarting_the_gateway_rolls_every_tenant_gateway(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(eks_deploy_task, "run", lambda args, **_: calls.append(args) or "")

    eks_deploy_task.restart(["mcp-gateway"], {"tenants": [{"name": "adp"}, {"name": "beta"}]})

    assert [c[1:6] for c in calls[:2]] == [
        ["-n", "tenant-adp", "rollout", "restart", "deployment/mcp-gateway"],
        ["-n", "tenant-beta", "rollout", "restart", "deployment/mcp-gateway"],
    ]
    assert not [c for c in calls if "agentic-dev-platform" in c]


def test_restarting_the_review_workers_rolls_each_tenants_review_worker(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(eks_deploy_task, "run", lambda args, **_: calls.append(args) or "")

    eks_deploy_task.restart(
        ["tenant-review-workers"], {"tenants": [{"name": "adp"}, {"name": "beta"}]}
    )

    assert [c[1:6] for c in calls[:2]] == [
        ["-n", "tenant-adp-review", "rollout", "restart", "deployment/review-worker"],
        ["-n", "tenant-beta-review", "rollout", "restart", "deployment/review-worker"],
    ]
    assert not [c for c in calls if "agentic-dev-platform" in c]


def test_the_platform_gateway_is_retired(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(eks_deploy_task, "run", lambda args, **_: calls.append(args) or "")

    eks_deploy_task.delete_retired()

    deleted = [c[1:6] for c in calls]
    for kind in ("deployment", "service", "ingress", "secretproviderclass", "serviceaccount"):
        assert ["-n", "agentic-dev-platform", "delete", kind, "mcp-gateway"] in deleted


def _gateway_ingress(tenant: str) -> dict:
    return {
        "kind": "Ingress",
        "metadata": {"name": "mcp-gateway", "namespace": f"tenant-{tenant}"},
        "spec": {"rules": [{"host": f"mcp-{tenant}.mcp.example.internal"}]},
    }


def _deploy_without_a_cluster(monkeypatch, balancers: dict[str, str]) -> list[list[str]]:
    rendered: list[dict] = []
    rolled: list[list[str]] = []

    def render(_overlay, inputs, _root, **_):
        rendered.append(inputs)
        return [_gateway_ingress(entry["name"]) for entry in inputs["tenants"]], []

    monkeypatch.setattr(eks_deploy_task, "install_controller", lambda inputs: None)
    monkeypatch.setattr(eks_deploy_task, "overlay_documents", list)
    monkeypatch.setattr(eks_deploy_task, "render", render)
    monkeypatch.setattr(eks_deploy_task, "apply", lambda documents: None)
    monkeypatch.setattr(eks_deploy_task, "delete_retired", lambda: None)
    monkeypatch.setattr(eks_deploy_task, "rollout", lambda targets: rolled.append(list(targets)))
    monkeypatch.setattr(eks_deploy_task, "report_unselected", lambda: [])
    monkeypatch.setattr(
        eks_deploy_task, "wait_for_hostname", lambda name, namespace: balancers[namespace]
    )
    eks_deploy_task.deploy({"tenants": [{"name": "adp"}, {"name": "beta"}]}, [])
    assert len(rendered) == 1
    return rolled


def test_each_tenant_gateway_is_rendered_once_and_its_load_balancer_awaited(
    monkeypatch, capsys
) -> None:
    rolled = _deploy_without_a_cluster(
        monkeypatch, {"tenant-adp": "adp.elb.example", "tenant-beta": "beta.elb.example"}
    )

    assert "tenant-adp/mcp-gateway" in rolled[0] and "tenant-beta/mcp-gateway" in rolled[0]
    assert "tenant-adp-review/review-worker" in rolled[0]
    assert "tenant-beta-review/review-worker" in rolled[0]
    assert "mcp-gateway" not in rolled[0]
    assert len(rolled) == 1
    out = capsys.readouterr().out
    assert "https://mcp-adp.mcp.example.internal/mcp from adp.elb.example" in out
    assert "https://mcp-beta.mcp.example.internal/mcp from beta.elb.example" in out


def test_a_gateway_load_balancer_that_never_appears_fails_the_deploy(monkeypatch) -> None:
    with pytest.raises(eks_deploy_task.DeployError, match="beta mcp-gateway load balancer"):
        _deploy_without_a_cluster(monkeypatch, {"tenant-adp": "adp.elb.example", "tenant-beta": ""})


def test_the_retired_objects_are_deleted_when_present(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(eks_deploy_task, "run", lambda args, **_: calls.append(args) or "")

    eks_deploy_task.delete_retired()

    assert ["delete", "namespace", "agent-sandbox", "--ignore-not-found", "--wait=false"] in [
        c[1:] for c in calls
    ]
    assert all("--ignore-not-found" in c for c in calls)
    assert ["-n", "agentic-dev-platform", "delete", "deployment", "activity-worker"] in [
        c[1:6] for c in calls
    ]


def test_the_mounted_secrets_cover_every_provider_object() -> None:
    keys = eks_deploy.mounted_secret_keys()

    assert "database_platform_app" in keys
    assert "temporal_ca" in keys
    assert "mcp_gateway_token" in keys


def test_a_key_every_tenant_holds_needs_no_platform_secret() -> None:
    tenants = [
        {"name": "adp", "secrets": {"mcp_gateway_token": "arn:adp", "github_token": "arn:a"}},
        {"name": "beta", "secrets": {"mcp_gateway_token": "arn:beta"}},
    ]
    mounted = ["github_token", "jira_pat", "mcp_gateway_token"]

    keys = eks_deploy.platform_mounted_keys(mounted, {"github_token": "arn:gh"}, tenants)

    assert keys == ["github_token", "jira_pat"]


def test_a_key_one_tenant_lacks_still_needs_a_platform_secret() -> None:
    tenants = [
        {"name": "adp", "secrets": {"mcp_gateway_token": "arn:adp"}},
        {"name": "beta", "secrets": {}},
    ]

    keys = eks_deploy.platform_mounted_keys(["mcp_gateway_token"], {}, tenants)

    assert keys == ["mcp_gateway_token"]


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


def test_tenant_secrets_without_a_current_value_are_named_by_tenant() -> None:
    tenants = [{"name": "adp", "secrets": {"github_token": "arn:gh", "jira_pat": "arn:pat"}}]
    secrets = _FakeSecrets({"arn:gh": True, "arn:pat": False})

    assert eks_deploy.unset_tenant_secrets(tenants, secrets) == ["adp:jira_pat"]


def test_an_unset_review_signing_key_names_the_review_worker_that_cannot_start() -> None:
    tenants = [
        {"name": name, "secrets": {"review_signing_key": f"arn:{name}-review"}}
        for name in ("adp", "beta")
    ]
    secrets = _FakeSecrets({"arn:adp-review": True, "arn:beta-review": False})
    mounted = eks_deploy.mounted_secret_keys()

    assert "review_signing_key" in mounted
    assert "review_signing_key" not in eks_deploy.platform_mounted_keys(mounted, {}, tenants)
    assert eks_deploy.unset_tenant_secrets(tenants, secrets) == ["beta:review_signing_key"]
    message = eks_deploy.unset_message("beta:review_signing_key")
    assert "review worker of beta refuses to start" in message
    assert "trust root under tenant beta" in message
    assert "start without it" in eks_deploy.unset_message("beta:jira_pat")


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


def test_apply_sends_each_workload_after_the_secret_providers_its_pods_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def run(args: list[str], *, stdin: str | None = None, timeout: int = 0) -> str:
        sent.append(stdin or "")
        return ""

    monkeypatch.setattr(eks_deploy_task, "run", run)

    eks_deploy_task.apply(
        [
            {"kind": "Deployment", "metadata": {"name": "inbound-webhook"}},
            {"kind": "SecretProviderClass", "metadata": {"name": "inbound-webhook"}},
            {"kind": "ConfigMap", "metadata": {"name": "inbound-webhook-tenant-config"}},
        ]
    )

    kinds = [item["kind"] for item in json.loads(sent[0])["items"]]
    assert kinds == ["SecretProviderClass", "ConfigMap", "Deployment"]


@pytest.mark.parametrize(
    ("names", "host", "covered"),
    [
        (["*.mcp.example.internal"], "mcp-adp.mcp.example.internal", True),
        (["mcp-adp.mcp.example.internal"], "mcp-adp.mcp.example.internal", True),
        (["*.MCP.example.internal."], "mcp-adp.mcp.example.internal", True),
        (["*.example.internal"], "mcp-adp.mcp.example.internal", False),
        (["*.us-east-1.elb.amazonaws.com"], "mcp-adp.mcp.example.internal", False),
        (["mcp-beta.mcp.example.internal"], "mcp-adp.mcp.example.internal", False),
    ],
)
def test_a_wildcard_certificate_covers_one_label(names, host, covered) -> None:
    assert eks_deploy.certificate_covers(names, host) is covered


class _FakeAcm:
    def __init__(self, domain: str, alternatives: list[str]) -> None:
        self.certificate = {"DomainName": domain, "SubjectAlternativeNames": alternatives}

    def describe_certificate(self, CertificateArn):  # noqa: N803 - the boto3 parameter name
        return {"Certificate": self.certificate}


HOSTS = {"adp": "mcp-adp.mcp.example.internal", "beta": "mcp-beta.mcp.example.internal"}


def test_a_certificate_that_misses_a_gateway_host_is_refused() -> None:
    acm = _FakeAcm("mcp-adp.mcp.example.internal", ["mcp-adp.mcp.example.internal"])

    with pytest.raises(eks_deploy.DeployError, match="not mcp-beta.mcp.example.internal"):
        eks_deploy.check_gateway_certificate(acm, "arn:cert", HOSTS)


def test_a_wildcard_certificate_for_the_domain_is_accepted() -> None:
    acm = _FakeAcm("*.mcp.example.internal", ["*.mcp.example.internal"])

    eks_deploy.check_gateway_certificate(acm, "arn:cert", HOSTS)


def _balancer(name: str, vpc: str = "vpc-1") -> dict:
    return {
        "LoadBalancerArn": f"arn:lb/{name}",
        "DNSName": f"internal-{name}.us-east-1.elb.amazonaws.com",
        "CanonicalHostedZoneId": "Z35SXDOTRQ7X7K",
        "VpcId": vpc,
    }


class _FakeElb:
    def __init__(self, balancers: list[dict], tags: dict[str, dict[str, str]]) -> None:
        self.balancers = balancers
        self.tags = tags

    def get_paginator(self, name):
        assert name == "describe_load_balancers"
        balancers = self.balancers

        class _Pages:
            def paginate(self):
                yield {"LoadBalancers": balancers}

        return _Pages()

    def describe_tags(self, ResourceArns):  # noqa: N803 - the boto3 parameter name
        assert len(ResourceArns) <= 20
        return {
            "TagDescriptions": [
                {
                    "ResourceArn": arn,
                    "Tags": [{"Key": k, "Value": v} for k, v in self.tags.get(arn, {}).items()],
                }
                for arn in ResourceArns
            ]
        }


def _stack(namespace: str, cluster: str = "platform-eks") -> dict[str, str]:
    return {"elbv2.k8s.aws/cluster": cluster, "ingress.k8s.aws/stack": f"{namespace}/mcp-gateway"}


def test_each_gateway_load_balancer_is_found_by_its_ingress_tags() -> None:
    elb = _FakeElb(
        [
            _balancer("adp"),
            _balancer("beta"),
            _balancer("other-cluster"),
            _balancer("webhook"),
            _balancer("other-vpc", vpc="vpc-2"),
        ],
        {
            "arn:lb/adp": _stack("tenant-adp"),
            "arn:lb/beta": _stack("tenant-beta"),
            "arn:lb/other-cluster": _stack("tenant-adp", cluster="other"),
            "arn:lb/webhook": {
                "elbv2.k8s.aws/cluster": "platform-eks",
                "ingress.k8s.aws/stack": "agentic-dev-platform/inbound-webhook",
            },
            "arn:lb/other-vpc": _stack("tenant-beta"),
        },
    )

    found = eks_deploy.gateway_load_balancers(elb, "platform-eks", "vpc-1", ["adp", "beta"])

    assert {tenant: b["LoadBalancerArn"] for tenant, b in found.items()} == {
        "adp": "arn:lb/adp",
        "beta": "arn:lb/beta",
    }


class _FakeRoute53:
    def __init__(self) -> None:
        self.changes: list[dict] = []

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):  # noqa: N803
        self.changes.append({"zone": HostedZoneId, **ChangeBatch})
        return {"ChangeInfo": {"Id": "/change/1", "Status": "PENDING"}}


def test_each_gateway_host_is_an_alias_of_its_tenants_load_balancer() -> None:
    route53 = _FakeRoute53()
    balancers = {"adp": _balancer("adp"), "beta": _balancer("beta")}

    eks_deploy.publish_gateway_hosts(route53, "Z0MCP", HOSTS, balancers)

    assert len(route53.changes) == 1
    assert route53.changes[0]["zone"] == "Z0MCP"
    records = [change["ResourceRecordSet"] for change in route53.changes[0]["Changes"]]
    assert {change["Action"] for change in route53.changes[0]["Changes"]} == {"UPSERT"}
    assert records == [
        {
            "Name": f"mcp-{tenant}.mcp.example.internal",
            "Type": "A",
            "AliasTarget": {
                "HostedZoneId": "Z35SXDOTRQ7X7K",
                "DNSName": f"internal-{tenant}.us-east-1.elb.amazonaws.com",
                "EvaluateTargetHealth": False,
            },
        }
        for tenant in ("adp", "beta")
    ]


def test_a_gateway_without_a_load_balancer_writes_no_record() -> None:
    route53 = _FakeRoute53()

    with pytest.raises(eks_deploy.DeployError, match="MCP gateway of beta"):
        eks_deploy.publish_gateway_hosts(route53, "Z0MCP", HOSTS, {"adp": _balancer("adp")})
    assert route53.changes == []


class _FakeSession:
    def __init__(self, clients: dict) -> None:
        self.clients = clients
        self.order: list[str] = []

    def client(self, name):
        self.order.append(name)
        return self.clients[name]


def _eks() -> dict:
    return {
        "deployer": DEPLOYER,
        "cluster_name": "platform-eks",
        "vpc_id": "vpc-1",
        "secret_arns": {},
        "tenants": [],
        "mcp_gateway_certificate_arn": "arn:cert",
        "mcp_gateway_zone_id": "Z0MCP",
        "mcp_gateway_hosts": HOSTS,
    }


def test_the_deploy_checks_the_certificate_then_names_each_gateway(monkeypatch) -> None:
    monkeypatch.setattr(eks_deploy, "mounted_secret_keys", lambda: [])
    route53 = _FakeRoute53()
    session = _FakeSession(
        {
            "acm": _FakeAcm("*.mcp.example.internal", []),
            "secretsmanager": _FakeSecrets({}),
            "ecs": _FakeEcs(0),
            "logs": _FakeLogs(),
            "elbv2": _FakeElb(
                [_balancer("adp"), _balancer("beta")],
                {"arn:lb/adp": _stack("tenant-adp"), "arn:lb/beta": _stack("tenant-beta")},
            ),
            "route53": route53,
        }
    )

    assert eks_deploy.deploy(session, _eks(), ["deploy"]) == 0

    assert session.order.index("acm") < session.order.index("ecs")
    assert session.order.index("ecs") < session.order.index("route53")
    names = [change["ResourceRecordSet"]["Name"] for change in route53.changes[0]["Changes"]]
    assert names == list(HOSTS.values())


def test_an_uncovered_gateway_host_stops_the_deploy_before_the_task(monkeypatch) -> None:
    ecs = _FakeEcs(0)
    session = _FakeSession({"acm": _FakeAcm("*.us-east-1.elb.amazonaws.com", []), "ecs": ecs})

    with pytest.raises(eks_deploy.DeployError, match="MCP gateway certificate"):
        eks_deploy.deploy(session, _eks(), ["deploy"])
    assert ecs.started == {}


def test_a_restart_leaves_the_gateway_names_alone() -> None:
    session = _FakeSession({"ecs": _FakeEcs(0), "logs": _FakeLogs()})

    assert eks_deploy.deploy(session, _eks(), ["restart", "mcp-gateway"]) == 0
    assert session.order == ["ecs", "logs"]


class _Kubectl:
    """Deployments and pods keyed by `namespace/name`, served to the kubectl calls of the task."""

    def __init__(self, replicas: dict[str, int], pods: dict[str, list[str]] | None = None) -> None:
        self.deployments = {
            target: {
                "metadata": {"annotations": {}},
                "spec": {"replicas": count, "selector": {"matchLabels": {"app": target}}},
            }
            for target, count in replicas.items()
        }
        self.pods = pods or {}
        self.rolled: list[str] = []

    def __call__(self, args: list[str], **_: object) -> str:
        namespace, verb = args[2], args[3]
        if verb == "get" and args[4] == "deployment":
            deployment = self.deployments.get(f"{namespace}/{args[5]}")
            return json.dumps(deployment) if deployment else ""
        if verb == "get" and args[4] == "pods":
            target = args[6].removeprefix("app=")
            items = [
                {"metadata": {"name": pod}, "status": {"phase": "Running"}}
                for pod in self.pods.get(target, [])
            ]
            return json.dumps({"items": items})
        if verb == "patch":
            deployment = self.deployments[f"{namespace}/{args[4].removeprefix('deployment/')}"]
            patch = json.loads(args[-1])
            annotations = deployment["metadata"]["annotations"]
            for key, value in patch["metadata"]["annotations"].items():
                if value is None:
                    annotations.pop(key, None)
                else:
                    annotations[key] = value
            deployment["spec"]["replicas"] = patch["spec"]["replicas"]
            return ""
        if verb == "rollout":
            self.rolled.append(f"{namespace}/{args[5].removeprefix('deployment/')}")
            return ""
        raise AssertionError(args)

    def replicas(self) -> dict[str, int]:
        return {target: d["spec"]["replicas"] for target, d in self.deployments.items()}

    def recorded(self) -> dict[str, str]:
        annotation = eks_deploy_task.REPLICAS_ANNOTATION
        return {
            target: d["metadata"]["annotations"][annotation]
            for target, d in self.deployments.items()
            if annotation in d["metadata"]["annotations"]
        }


TENANT = {"tenants": [{"name": "adp"}]}


def test_scale_down_records_each_running_deployment_and_keeps_an_earlier_record(
    monkeypatch,
) -> None:
    kubectl = _Kubectl({"tenant-adp/activity-worker": 2, "agentic-dev-platform/litellm": 0})
    kubectl.deployments["agentic-dev-platform/litellm"]["metadata"]["annotations"][
        eks_deploy_task.REPLICAS_ANNOTATION
    ] = "1"
    monkeypatch.setattr(eks_deploy_task, "run", kubectl)

    running = eks_deploy_task.scale_down(["tenant-workers", "litellm", "inbound-webhook"], TENANT)

    assert running == []
    assert kubectl.replicas() == {
        "tenant-adp/activity-worker": 0,
        "agentic-dev-platform/litellm": 0,
    }
    assert kubectl.recorded() == {
        "tenant-adp/activity-worker": "2",
        "agentic-dev-platform/litellm": "1",
    }


def test_scale_up_restores_only_the_recorded_deployments_and_forgets_the_record(
    monkeypatch,
) -> None:
    kubectl = _Kubectl({"tenant-adp/activity-worker": 0, "agentic-dev-platform/litellm": 0})
    kubectl.deployments["tenant-adp/activity-worker"]["metadata"]["annotations"][
        eks_deploy_task.REPLICAS_ANNOTATION
    ] = "2"
    monkeypatch.setattr(eks_deploy_task, "run", kubectl)

    eks_deploy_task.scale_up(["tenant-workers", "litellm"], TENANT)

    assert kubectl.replicas() == {
        "tenant-adp/activity-worker": 2,
        "agentic-dev-platform/litellm": 0,
    }
    assert kubectl.recorded() == {}
    assert kubectl.rolled == ["tenant-adp/activity-worker"]


def test_check_stopped_names_each_running_pod_and_fails(monkeypatch, capsys) -> None:
    kubectl = _Kubectl(
        {"tenant-adp/activity-worker": 0, "agentic-dev-platform/inbound-webhook": 0},
        pods={"agentic-dev-platform/inbound-webhook": ["inbound-webhook-7d9-abc"]},
    )
    monkeypatch.setattr(eks_deploy_task, "run", kubectl)
    monkeypatch.setattr(eks_deploy_task, "write_kubeconfig", lambda cluster, path: None)
    monkeypatch.setenv("DEPLOY_INPUTS", json.dumps({"cluster": {}, **TENANT}))
    monkeypatch.setenv("KUBECONFIG", "")

    exit_code = eks_deploy_task.main(["check-stopped", "tenant-workers", "inbound-webhook"])

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{eks_deploy_task.RUNNING_POD}agentic-dev-platform/inbound-webhook-7d9-abc"
    ]
