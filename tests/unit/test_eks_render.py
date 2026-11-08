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
        "workspace_volume_handle": "fs-1::fsap-1",
        "rebar_roles": {"reader": "arn:aws:iam::9:role/r", "writer": "arn:aws:iam::9:role/w"},
        "secrets": {key: f"arn:aws:secretsmanager:us-west-2:9:secret:{key}" for key in keys},
        "vpc_cidr": "10.42.0.0/16",
        "service_cidr": "10.100.0.0/16",
        "gate_egress_cidrs": ["10.42.1.0/24", "52.216.0.0/15"],
        "ingress": {
            "webhook_certificate_arn": "arn:aws:acm:us-west-2:9:certificate/webhook",
            "mcp_gateway_certificate_arn": "",
            "mcp_gateway_client_cidrs": [],
        },
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


def _named(documents, kind: str, name: str) -> dict[str, Any]:
    return next(d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name)


def test_every_synthetic_value_is_replaced(overlay) -> None:
    documents, skipped = eks_render.render(overlay, _inputs(), REPOSITORY)
    text = json.dumps(documents)

    assert skipped == []
    assert "123456789012" not in text
    assert "fs-00000000000000000" not in text
    assert "sg-0000000000000000" not in text
    assert "agentic-platform/external/" not in text
    assert '"10.0.0.0/16"' not in text and '"172.20.0.0/16"' not in text
    assert _named(documents, "PersistentVolume", "agentic-platform-checkouts")["spec"]["csi"][
        "volumeHandle"
    ] == ("fs-1::fsap-1")
    groups = _named(documents, "SecurityGroupPolicy", "temporal-clients")["spec"]
    assert groups["securityGroups"]["groupIds"] == ["sg-c", "sg-t", "sg-i"]
    profiles = _named(documents, "ConfigMap", "rebar-store-aws-config")["data"]["config"]
    assert "role_arn = arn:aws:iam::9:role/w" in profiles
    assert "credential_source = EcsContainer" in profiles


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
        overlay, _inputs(), REPOSITORY, omit_secrets=["github_token", "jira_webhook_secret"]
    )
    activity = json.loads(
        _named(documents, "SecretProviderClass", "activity-worker")["spec"]["parameters"]["objects"]
    )
    names = [entry["objectName"] for entry in activity]

    assert sorted(skipped) == [
        "activity-worker:GITHUB_TOKEN",
        "inbound-webhook:JIRA_WEBHOOK_SECRET",
    ]
    assert "arn:aws:secretsmanager:us-west-2:9:secret:temporal_ca" in names
    assert not [name for name in names if name.endswith("github_token")]
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
    egress = _named(documents, "NetworkPolicy", "gate-engine-egress")["spec"]["egress"]
    registry = next(rule for rule in egress if rule["ports"][0]["port"] == 443)

    assert [peer["ipBlock"]["cidr"] for peer in registry["to"]] == _inputs()["gate_egress_cidrs"]


def test_the_generated_config_maps_carry_the_environment(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)

    environment = _named(documents, "ConfigMap", "agentic-platform-env")["data"]
    assert environment["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert _named(documents, "ConfigMap", "platform-config")["data"] == {
        "repositories.yaml": "version: 1\n"
    }
    assert (
        "model_list"
        in _named(documents, "ConfigMap", "litellm-config")["data"]["litellm-config.yaml"]
    )
    assert _named(documents, "ConfigMap", "promotion-policies")["data"]
    eks = _named(documents, "ConfigMap", "agentic-platform-eks")["data"]
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
    egress = _named(documents, "NetworkPolicy", "gate-engine-egress")["spec"]["egress"]
    registry = next(rule for rule in egress if rule["ports"][0]["port"] == 443)

    assert {"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.42.0.0/16"]}} in registry["to"]
    assert [rule["ports"][0]["port"] for rule in egress] == [53, 4000, 443]
