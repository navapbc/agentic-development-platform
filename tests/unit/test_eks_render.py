"""The AWS overlay becomes one environment's manifests from the eks-deployer inputs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import eks_render
from services import process

REPOSITORY = Path(__file__).resolve().parents[2]
OVERLAY = REPOSITORY / "deploy" / "kubernetes" / "aws"
ECR = "999999999999.dkr.ecr.us-west-2.amazonaws.com/agentic-platform"

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl renders it")


def _inputs() -> dict[str, Any]:
    keys = {
        "database_platform_app",
        "coordination_shared_secret",
        "litellm_master_key",
        "review_attestation_secret",
        "authorship_signing_key",
        "github_token",
        "jira_api_token",
        "jira_pat",
        "slack_webhook_url",
        "registry_token",
        "github_webhook_secret",
        "jira_webhook_secret",
        "mcp_gateway_token",
        "langfuse_public_key",
        "langfuse_secret_key",
        "temporal_ca",
    }
    return {
        "images": {
            "platform": f"{ECR}/platform:1@sha256:{'a' * 64}",
            "opa": f"{ECR}/opa:1@sha256:{'b' * 64}",
            "litellm": f"{ECR}/litellm:1@sha256:{'c' * 64}",
        },
        "image_settings": {
            name: f"{ECR}/{name.lower()}:1"
            for name in (
                "OPENHANDS_AGENT_SERVER_IMAGE",
                "GATE_ENGINE_BUILDER_IMAGE",
                "SEMGREP_IMAGE",
                "TRIVY_IMAGE",
            )
        },
        "settings": {"LANGFUSE_HOST": "https://langfuse.example.internal"},
        "platform_environment": {"DATABASE_URL": "postgresql+asyncpg://db:5432/agent_platform"},
        "environment_config": {"repositories.yaml": "version: 1\n"},
        "security_groups": {
            "cluster": "sg-c",
            "temporal_client": "sg-t",
            "internal_client": "sg-i",
        },
        "secrets": {key: f"arn:aws:secretsmanager:us-west-2:9:secret:{key}" for key in keys},
        "tenants": [_tenant("adp")],
        "vpc_cidr": "10.42.0.0/16",
        "service_cidr": "10.100.0.0/16",
        "gate_egress_cidrs": ["10.42.1.0/24", "52.216.0.0/15"],
        "ingress": {
            "webhook_certificate_arn": "arn:aws:acm:us-west-2:9:certificate/webhook",
            "mcp_gateway_certificate_arn": "",
            "mcp_gateway_client_cidrs": [],
        },
    }


def _tenant(name: str) -> dict[str, Any]:
    own = ("github_token", "jira_api_token", "jira_pat", "authorship_signing_key", "registry_token")
    return {
        "name": name,
        "workspace_volume_handle": f"fs-1::fsap-{name}",
        "rebar_roles": {
            "reader": f"arn:aws:iam::9:role/{name}-r",
            "writer": f"arn:aws:iam::9:role/{name}-w",
        },
        "secrets": {
            key: f"arn:aws:secretsmanager:us-west-2:9:secret:tenants/{name}/{key}" for key in own
        },
        "platform_secrets": [
            "database_platform_app",
            "coordination_shared_secret",
            "litellm_master_key",
            "review_attestation_secret",
            "temporal_ca",
        ],
        "settings": {"TICKET_BACKEND": "rebar"},
        "sandbox_quota": {"requests.cpu": "8", "pods": "20"},
        "config_bucket": "agentic-platform-tenant-config-abc",
    }


@pytest.fixture(scope="module")
def overlay() -> list[dict[str, Any]]:
    rendered = process.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(OVERLAY)],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _named(documents, kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
    return next(
        d
        for d in documents
        if d["kind"] == kind
        and d["metadata"]["name"] == name
        and (namespace is None or d["metadata"].get("namespace", "") == namespace)
    )


def test_every_synthetic_value_is_replaced(overlay) -> None:
    documents, skipped = eks_render.render(overlay, _inputs(), REPOSITORY)
    text = json.dumps(documents)

    assert skipped == []
    assert "123456789012" not in text
    assert "fs-00000000000000000" not in text
    assert "sg-0000000000000000" not in text
    assert "agentic-platform/external/" not in text
    assert '"10.0.0.0/16"' not in text and '"172.20.0.0/16"' not in text
    volume = _named(documents, "PersistentVolume", "tenant-adp-workspace")
    assert volume["spec"]["csi"]["volumeHandle"] == "fs-1::fsap-adp"
    groups = _named(documents, "SecurityGroupPolicy", "temporal-clients", "tenant-adp")["spec"]
    assert groups["securityGroups"]["groupIds"] == ["sg-c", "sg-t", "sg-i"]
    profiles = _named(documents, "ConfigMap", "rebar-store-aws-config", "tenant-adp")["data"]
    assert "role_arn = arn:aws:iam::9:role/adp-w" in profiles["config"]
    assert "credential_source = EcsContainer" in profiles["config"]


def test_each_container_runs_the_published_image(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)
    images = {
        container["name"]: container["image"]
        for document in documents
        if document["kind"] == "Deployment"
        for container in [
            *document["spec"]["template"]["spec"].get("initContainers", []),
            *document["spec"]["template"]["spec"]["containers"],
        ]
    }

    assert images["activity-worker"] == images["mcp-gateway"] == _inputs()["images"]["platform"]
    assert images["opa"] == _inputs()["images"]["opa"]
    assert images["litellm"] == _inputs()["images"]["litellm"]


def test_providers_name_the_secret_arns_and_leave_out_unset_ones(overlay) -> None:
    documents, skipped = eks_render.render(
        overlay, _inputs(), REPOSITORY, omit_secrets=["adp:github_token", "jira_webhook_secret"]
    )
    provider = _named(documents, "SecretProviderClass", "activity-worker", "tenant-adp")
    activity = json.loads(provider["spec"]["parameters"]["objects"])
    names = [entry["objectName"] for entry in activity]

    assert sorted(skipped) == ["inbound-webhook:JIRA_WEBHOOK_SECRET", "tenant-adp:GITHUB_TOKEN"]
    assert "arn:aws:secretsmanager:us-west-2:9:secret:temporal_ca" in names
    assert "arn:aws:secretsmanager:us-west-2:9:secret:tenants/adp/jira_pat" in names
    assert not [name for name in names if name.endswith("github_token")]
    assert not [name for name in names if name.endswith("slack_webhook_url")]
    database = next(e for e in activity if e["objectName"].endswith("database_platform_app"))
    assert [p["objectAlias"] for p in database["jmesPath"]] == ["PGUSER", "PGPASSWORD"]


def test_an_ingress_without_a_certificate_is_left_out(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY, mcp_hostname="mcp.example")
    ingresses = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Ingress"}

    assert list(ingresses) == ["inbound-webhook"]
    annotations = ingresses["inbound-webhook"]["metadata"]["annotations"]
    assert annotations["alb.ingress.kubernetes.io/certificate-arn"].endswith("/webhook")


def test_the_gateway_accepts_only_its_load_balancer_host(overlay) -> None:
    inputs = _inputs()
    inputs["ingress"]["mcp_gateway_certificate_arn"] = "arn:aws:acm:us-west-2:9:certificate/mcp"
    inputs["ingress"]["mcp_gateway_client_cidrs"] = ["10.42.21.0/24"]
    documents, _ = eks_render.render(overlay, inputs, REPOSITORY, mcp_hostname="mcp.example")
    gateway = _named(documents, "Deployment", "mcp-gateway")["spec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in gateway["containers"][0]["env"]}
    ingress = _named(documents, "Ingress", "mcp-gateway")["metadata"]["annotations"]

    assert env["MCP_ALLOWED_HOSTS"] == "mcp.example,mcp.example:443"
    assert ingress["alb.ingress.kubernetes.io/inbound-cidrs"] == "10.42.21.0/24"


def test_gate_pods_reach_only_the_listed_registry_ranges(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)
    egress = _named(documents, "NetworkPolicy", "gate-engine-egress", "tenant-adp-sandbox")
    registry = next(rule for rule in egress["spec"]["egress"] if rule["ports"][0]["port"] == 443)

    assert [peer["ipBlock"]["cidr"] for peer in registry["to"]] == _inputs()["gate_egress_cidrs"]


def test_the_generated_config_maps_carry_the_environment(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)

    environment = _named(documents, "ConfigMap", "agentic-platform-env", "tenant-adp")["data"]
    assert environment["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert _named(documents, "ConfigMap", "platform-config", "tenant-adp")["data"] == {
        "repositories.yaml": "version: 1\n"
    }
    assert (
        "model_list"
        in _named(documents, "ConfigMap", "litellm-config")["data"]["litellm-config.yaml"]
    )
    assert _named(documents, "ConfigMap", "promotion-policies")["data"]
    eks = _named(documents, "ConfigMap", "agentic-platform-eks", "tenant-adp")["data"]
    assert eks["SEMGREP_IMAGE"] == f"{ECR}/semgrep_image:1"
    assert eks["LANGFUSE_HOST"] == "https://langfuse.example.internal"


def test_a_config_change_rolls_the_pods(overlay) -> None:
    def hashes(inputs):
        documents, _ = eks_render.render(overlay, inputs, REPOSITORY)
        return {
            d["metadata"]["name"]: d["spec"]["template"]["metadata"]["annotations"][
                eks_render.CONFIG_HASH
            ]
            for d in documents
            if d["kind"] == "Deployment"
        }

    changed = _inputs()
    changed["platform_environment"]["LOG_LEVEL"] = "DEBUG"

    assert hashes(_inputs()) == hashes(_inputs())
    assert hashes(_inputs())["activity-worker"] != hashes(changed)["activity-worker"]


def test_an_image_outside_the_overlay_registry_is_refused(overlay) -> None:
    inputs = _inputs()
    del inputs["images"]["opa"]

    with pytest.raises(eks_render.RenderError, match="no published image for opa"):
        eks_render.render(overlay, inputs, REPOSITORY)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("agentic-platform/database/platform-app", "database_platform_app"),
        ("agentic-platform/generated/mcp-gateway-token", "mcp_gateway_token"),
        ("agentic-platform/external/github-token", "github_token"),
        ("agentic-platform/temporal/tls-ca", "temporal_ca"),
    ],
)
def test_object_names_map_to_secret_store_keys(name, key) -> None:
    assert eks_render.secret_key(name) == key


def test_an_image_setting_left_synthetic_is_refused(overlay) -> None:
    inputs = _inputs()
    del inputs["image_settings"]["TRIVY_IMAGE"]

    with pytest.raises(eks_render.RenderError, match="no published image for TRIVY_IMAGE"):
        eks_render.render(overlay, inputs, REPOSITORY)


def test_the_aws_registry_mode_opens_the_gate_pods_to_addresses_outside_the_vpc(overlay) -> None:
    inputs = _inputs()
    inputs["gate_egress_internet"] = True
    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)
    policy = _named(documents, "NetworkPolicy", "gate-engine-egress", "tenant-adp-sandbox")
    egress = policy["spec"]["egress"]
    registry = next(rule for rule in egress if rule["ports"][0]["port"] == 443)

    assert {"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.42.0.0/16"]}} in registry["to"]
    assert [rule["ports"][0]["port"] for rule in egress] == [53, 4000, 443]


def test_each_tenant_gets_a_worker_namespace_and_a_sandbox_namespace(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"].append(_tenant("payments"))
    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)
    namespaces = {
        d["metadata"]["name"]: d["metadata"]["labels"]
        for d in documents
        if d["kind"] == "Namespace"
    }
    workers = {
        d["metadata"]["namespace"]
        for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "activity-worker"
    }

    assert {
        "tenant-adp",
        "tenant-adp-sandbox",
        "tenant-payments",
        "tenant-payments-sandbox",
    } <= set(namespaces)
    assert "agent-sandbox" not in namespaces
    assert namespaces["tenant-payments-sandbox"]["agentic-dev-platform/namespace-role"] == "sandbox"
    assert workers == {"tenant-adp", "tenant-payments"}
    assert not [d for d in documents if d["metadata"].get("namespace") == "agent-sandbox"]
    assert [d["kind"] for d in documents][: len(namespaces)] == ["Namespace"] * len(namespaces)


def test_a_tenant_worker_runs_on_its_namespaces_paths_and_settings(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)
    worker = _named(documents, "Deployment", "activity-worker", "tenant-adp")["spec"]["template"]
    container = next(c for c in worker["spec"]["containers"] if c["name"] == "activity-worker")
    env = {e["name"]: e.get("value") for e in container["env"]}
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}

    assert env["TENANT"] == "adp"
    assert (
        env["OPENHANDS_SANDBOX_NAMESPACE"] == env["GATE_ENGINE_NAMESPACE"] == "tenant-adp-sandbox"
    )
    assert env["REPOSITORY_ROOT"] == "/tenants/adp/repositories"
    assert env["TENANT_CONFIG_BUCKET"] == "agentic-platform-tenant-config-abc"
    assert env["TICKET_BACKEND"] == "rebar"
    assert env["LITELLM_BASE_URL"] == "http://litellm.agentic-dev-platform.svc.cluster.local:4000"
    assert mounts["workspaces"] == "/tenants/adp"
    assert worker["metadata"]["labels"]["agentic-dev-platform/tenant"] == "adp"
    claim = _named(documents, "PersistentVolumeClaim", "workspaces", "tenant-adp")
    assert claim["spec"]["volumeName"] == "tenant-adp-workspace"


def test_only_a_tenants_worker_reaches_its_sandboxes(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)
    ingress = _named(documents, "NetworkPolicy", "openhands-sandbox-ingress", "tenant-adp-sandbox")
    binding = _named(documents, "RoleBinding", "activity-worker-sandbox", "tenant-adp-sandbox")
    baseline = _named(documents, "NetworkPolicy", "platform-baseline", "tenant-adp")
    litellm = _named(documents, "NetworkPolicy", "litellm-ingress", "agentic-dev-platform")
    quota = _named(documents, "ResourceQuota", "sandbox", "tenant-adp-sandbox")

    peer = ingress["spec"]["ingress"][0]["from"][0]
    assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "tenant-adp"
    assert binding["subjects"][0]["namespace"] == "tenant-adp"
    reaches = [peer for rule in baseline["spec"]["egress"] for peer in rule.get("to", [])]
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tenant-adp-sandbox"}},
        "podSelector": {"matchLabels": {"app": "openhands-sandbox"}},
    } in reaches
    assert {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "agentic-dev-platform"}
        },
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
    } in reaches
    sources = litellm["spec"]["ingress"][0]["from"]
    assert {
        "namespaceSelector": {
            "matchExpressions": [{"key": "agentic-dev-platform/tenant", "operator": "Exists"}]
        }
    } in sources
    assert quota["spec"]["hard"] == {"requests.cpu": "8", "pods": "20"}


def test_a_tenant_mounts_no_other_tenants_secrets(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"].append(_tenant("payments"))
    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)
    provider = _named(documents, "SecretProviderClass", "activity-worker", "tenant-payments")
    names = [entry["objectName"] for entry in json.loads(provider["spec"]["parameters"]["objects"])]

    assert not [name for name in names if "/adp/" in name]
    assert "arn:aws:secretsmanager:us-west-2:9:secret:tenants/payments/github_token" in names


def test_a_deployment_without_tenants_is_refused(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = []

    with pytest.raises(eks_render.RenderError, match="name no tenant"):
        eks_render.render(overlay, inputs, REPOSITORY)
