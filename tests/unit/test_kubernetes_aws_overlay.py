"""The AWS overlay mounts the EKS workload secrets from Secrets Manager, never a Secret."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.tenant_render import tenant_root, worker_env
from services import process

REPOSITORY = Path(__file__).resolve().parents[2]
OVERLAY = REPOSITORY / "deploy" / "kubernetes" / "aws"
ROOT = REPOSITORY / "infra" / "terraform" / "examples" / "private-bedrock"
EKS_ROOT = ROOT / "eks.tf"
SERVICES = REPOSITORY / "infra" / "terraform" / "container-services" / "services.tf"
PLATFORM_WORKLOADS = ("activity-worker", "inbound-webhook", "mcp-gateway")
WORKLOADS = (*PLATFORM_WORKLOADS, "litellm")
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/"

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl renders it")


@pytest.fixture(scope="module")
def documents() -> list[dict[str, Any]]:
    rendered = process.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(OVERLAY)],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _named(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name)


def _pod(documents: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return _named(documents, "Deployment", name)["spec"]["template"]["spec"]


def _container(documents: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(c for c in _pod(documents, name)["containers"] if c["name"] == name)


def _terraform_secret_keys() -> dict[str, set[str]]:
    """Read the secret store keys each service account reads from the eks_workloads local."""
    text = EKS_ROOT.read_text(encoding="utf-8")
    pattern = re.compile(r'service_account = "([a-z-]+)"\n.*?secrets\s+= \[(.*?)\]', re.DOTALL)
    return {
        account: set(re.findall(r'"([a-z_]+)"', secrets))
        for account, secrets in pattern.findall(text)
    }


def _provider_keys(documents: list[dict[str, Any]], name: str) -> set[str]:
    """Map each Secrets Manager name a SecretProviderClass mounts to its secret store key.

    The names follow <prefix>/<group>/<key>, and the database group drops the database_ prefix
    of its keys. The Temporal CA belongs to the control plane, not to the secret store.
    """
    provider = _named(documents, "SecretProviderClass", name)
    keys = set()
    for entry in yaml.safe_load(provider["spec"]["parameters"]["objects"]):
        _, group, suffix = entry["objectName"].split("/")
        if group != "temporal":
            keys.add(("database_" if group == "database" else "") + suffix.replace("-", "_"))
    return keys


def test_no_rendered_object_is_a_secret(documents) -> None:
    assert not [d["metadata"]["name"] for d in documents if d["kind"] == "Secret"]


def test_the_review_worker_stays_on_ecs(documents) -> None:
    assert not [d for d in documents if d["metadata"]["name"] == "review-worker"]


def _tenant_worker_secret_keys() -> set[str]:
    """Read the platform secrets every tenant worker reads and a tenant's default own secrets."""
    platform = re.search(
        r"tenant_platform_secrets = \[\s*for key in \[(.*?)\]", (ROOT / "tenants.tf").read_text()
    )
    own = re.search(
        r"secrets\s+= optional\(list\(string\), \[(.*?)\]\)", (ROOT / "variables.tf").read_text()
    )
    return {key for match in (platform, own) for key in re.findall(r'"([a-z_]+)"', match.group(1))}


def test_every_workload_runs_as_the_service_account_its_pod_identity_binds(documents) -> None:
    terraform = _terraform_secret_keys()

    for name in WORKLOADS:
        assert _pod(documents, name)["serviceAccountName"] == name
        assert name in terraform or name == "activity-worker"
        _named(documents, "ServiceAccount", name)
    assert 'service_account = "activity-worker"' in (ROOT / "tenants.tf").read_text()


def test_each_workload_mounts_the_secrets_its_role_may_read(documents) -> None:
    terraform = {**_terraform_secret_keys(), "activity-worker": _tenant_worker_secret_keys()}

    for name in WORKLOADS:
        volume = next(v for v in _pod(documents, name)["volumes"] if v["name"] == "secrets")
        assert volume["csi"]["driver"] == "secrets-store.csi.k8s.io"
        assert volume["csi"]["volumeAttributes"]["secretProviderClass"] == name
        assert _provider_keys(documents, name) == terraform[name], name
        provider = _named(documents, "SecretProviderClass", name)
        assert provider["spec"]["provider"] == "aws"
        assert provider["spec"]["parameters"]["usePodIdentity"] == "true"


def test_no_container_reads_a_kubernetes_secret(documents) -> None:
    for name in WORKLOADS:
        container = _container(documents, name)
        assert not [e for e in container.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})]
        assert not [e for e in container.get("envFrom", []) if "secretRef" in e]
    for name in PLATFORM_WORKLOADS:
        env = {e["name"]: e.get("value") for e in _container(documents, name)["env"]}
        assert env["PLATFORM_SECRETS_DIR"] == "/mnt/secrets"
        assert "command" not in _container(documents, name)


def test_litellm_takes_only_the_settings_it_names(documents) -> None:
    container = _container(documents, "litellm")

    assert not container.get("envFrom")
    assert "DATABASE_URL" not in {e["name"] for e in container["env"]}
    assert container["command"][:2] == ["/bin/sh", "-c"]


def test_every_image_comes_from_ecr(documents) -> None:
    for name in WORKLOADS:
        pod = _pod(documents, name)
        for container in pod["containers"] + pod.get("initContainers", []):
            assert container["image"].startswith(REGISTRY), container["image"]
    settings = _named(documents, "ConfigMap", "agentic-platform-eks")["data"]
    for key in ("OPENHANDS_AGENT_SERVER_IMAGE", "GATE_ENGINE_BUILDER_IMAGE", "SEMGREP_IMAGE"):
        assert settings[key].startswith(REGISTRY)
    assert settings["SANDBOX_NODE_POOL"] == "sandbox"


def test_the_tenant_workspace_sits_where_the_review_worker_reads_it(documents) -> None:
    review_path = re.search(r'tenants_path\s+= "([^"]+)"', SERVICES.read_text()).group(1)
    claim = _named(documents, "PersistentVolumeClaim", "workspaces")
    volume = _named(documents, "PersistentVolume", claim["spec"]["volumeName"])
    env = worker_env("adp")

    assert claim["spec"]["storageClassName"] == ""
    assert volume["spec"]["csi"]["driver"] == "efs.csi.aws.com"
    assert "iam" in volume["spec"]["mountOptions"] and "tls" in volume["spec"]["mountOptions"]
    assert tenant_root("adp") == f"{review_path}/adp"
    assert env["REPOSITORY_ROOT"].startswith(f"{review_path}/adp/")
    assert env["EXECUTION_WORKSPACE_ROOT"].startswith(f"{review_path}/adp/")


def test_the_temporal_clients_carry_the_client_security_groups(documents) -> None:
    temporal = _named(documents, "SecurityGroupPolicy", "temporal-clients")["spec"]
    internal = _named(documents, "SecurityGroupPolicy", "internal-clients")["spec"]

    assert set(temporal["podSelector"]["matchExpressions"][0]["values"]) == {
        "activity-worker",
        "inbound-webhook",
    }
    assert len(temporal["securityGroups"]["groupIds"]) == 3
    assert set(internal["podSelector"]["matchExpressions"][0]["values"]) == {
        "litellm",
        "mcp-gateway",
    }


def test_gate_pods_reach_only_dns_litellm_the_registry_and_the_pod_identity_agent(
    documents,
) -> None:
    egress = _named(documents, "NetworkPolicy", "gate-engine-egress")["spec"]["egress"]
    ports = sorted((port["protocol"], port["port"]) for rule in egress for port in rule["ports"])

    assert ports == [("TCP", 53), ("TCP", 80), ("TCP", 443), ("TCP", 4000), ("UDP", 53)]
    agent = next(rule for rule in egress if rule["ports"][0]["port"] == 80)
    assert agent["to"] == [{"ipBlock": {"cidr": "169.254.170.23/32"}}]
    registry = next(rule for rule in egress if rule["ports"][0]["port"] == 443)
    assert all("except" not in peer["ipBlock"] for peer in registry["to"])
    assert "0.0.0.0/0" not in {peer["ipBlock"]["cidr"] for peer in registry["to"]}
