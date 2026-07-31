"""OpenHands sandbox manifests: image pinning, default-deny egress, and allowed targets."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from openhands.runtime.provisioner import SandboxSpec, WorkspaceBinding, build_pod_manifest

MANIFESTS = Path(__file__).resolve().parents[2] / "openhands"
POD = MANIFESTS / "images" / "agent-server-pod.yaml"
POLICIES = MANIFESTS / "policies"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_pod_pins_agent_server_image_and_hardens_the_container() -> None:
    pod = _load(POD)
    container = pod["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/openhands/agent-server:latest-python"
    assert ":" in container["image"].split("/")[-1]
    assert container["ports"][0]["containerPort"] == 8000
    assert pod["metadata"]["labels"]["app"] == "openhands-sandbox"
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_pod_builder_matches_the_declarative_manifest() -> None:
    pod = _load(POD)
    built = build_pod_manifest(
        SandboxSpec(
            execution_id="exec-1",
            image=pod["spec"]["containers"][0]["image"],
            litellm_base_url="http://litellm:4000",
        )
    )
    assert built["spec"]["containers"][0]["image"] == pod["spec"]["containers"][0]["image"]
    assert built["metadata"]["labels"]["app"] == pod["metadata"]["labels"]["app"]
    assert (
        built["spec"]["containers"][0]["ports"][0]["containerPort"]
        == pod["spec"]["containers"][0]["ports"][0]["containerPort"]
    )
    assert "runtimeClassName" not in built["spec"]


def test_builder_swaps_runtime_class_without_touching_the_pod_shape() -> None:
    hardened = build_pod_manifest(
        SandboxSpec(execution_id="exec-1", image="img", runtime_class="gvisor")
    )
    assert hardened["spec"]["runtimeClassName"] == "gvisor"


def test_pod_without_workspace_binding_declares_no_volumes() -> None:
    built = build_pod_manifest(SandboxSpec(execution_id="exec-1", image="img"))
    container = built["spec"]["containers"][0]
    assert "volumes" not in built["spec"]
    assert "volumeMounts" not in container
    assert "workingDir" not in container


def test_pod_binds_the_materialized_workspace_from_a_claim() -> None:
    built = build_pod_manifest(
        SandboxSpec(
            execution_id="exec-1",
            image="img",
            workspace=WorkspaceBinding(
                sub_path="agentic-demo-service/T-1/exec-1",
                claim_name="agent-workspaces",
            ),
        )
    )
    container = built["spec"]["containers"][0]
    volume = built["spec"]["volumes"][0]
    mount = container["volumeMounts"][0]
    assert volume["persistentVolumeClaim"]["claimName"] == "agent-workspaces"
    assert volume["name"] == mount["name"] == "agent-workspace"
    assert mount["subPath"] == "agentic-demo-service/T-1/exec-1"
    assert mount["mountPath"] == "/workspace"
    assert container["workingDir"] == "/workspace"
    assert {"name": "WORKSPACE_BASE", "value": "/workspace"} in container["env"]


def test_pod_binds_the_materialized_workspace_from_a_host_path() -> None:
    built = build_pod_manifest(
        SandboxSpec(
            execution_id="exec-1",
            image="img",
            workspace=WorkspaceBinding(
                sub_path="agentic-demo-service/T-1/exec-1",
                mount_path="/repo",
                host_path="/data/agent-workspaces",
            ),
        )
    )
    container = built["spec"]["containers"][0]
    volume = built["spec"]["volumes"][0]
    assert volume["hostPath"] == {"path": "/data/agent-workspaces", "type": "Directory"}
    assert container["volumeMounts"][0]["mountPath"] == "/repo"
    assert container["workingDir"] == "/repo"


def test_workspace_binding_requires_a_source() -> None:
    with pytest.raises(ValueError):
        build_pod_manifest(
            SandboxSpec(
                execution_id="exec-1",
                image="img",
                workspace=WorkspaceBinding(sub_path="agentic-demo-service/T-1/exec-1"),
            )
        )


def test_default_deny_policy_blocks_all_egress() -> None:
    policy = _load(POLICIES / "networkpolicy-default-deny-egress.yaml")
    assert policy["kind"] == "NetworkPolicy"
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["spec"]["egress"] == []
    assert policy["spec"]["podSelector"]["matchLabels"]["app"] == "openhands-sandbox"


def test_allow_policy_permits_only_dns_litellm_and_mediators() -> None:
    policy = _load(POLICIES / "networkpolicy-allow-egress.yaml")
    assert policy["spec"]["policyTypes"] == ["Egress"]
    selectors = []
    for rule in policy["spec"]["egress"]:
        for target in rule["to"]:
            pod_selector = target.get("podSelector", {}).get("matchLabels", {})
            selectors.append(pod_selector)
    assert {"k8s-app": "kube-dns"} in selectors
    assert {"app": "litellm"} in selectors
    assert {"agentic-dev-platform/mediator": "true"} in selectors


def test_security_group_policy_targets_the_sandbox_pods() -> None:
    policy = _load(POLICIES / "securitygrouppolicy.yaml")
    assert policy["kind"] == "SecurityGroupPolicy"
    assert policy["apiVersion"].startswith("vpcresources.k8s.aws/")
    assert policy["spec"]["podSelector"]["matchLabels"]["app"] == "openhands-sandbox"
