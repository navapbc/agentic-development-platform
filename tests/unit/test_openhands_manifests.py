"""OpenHands sandbox manifests: image pinning, default-deny egress, and allowed targets."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
from openhands.runtime.provisioner import (
    NODE_POOL_LABEL,
    SANDBOX_TAINT_KEY,
    SandboxSpec,
    WorkspaceBinding,
    _free_port,
    _http_ready,
    build_pod_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = REPO_ROOT / "openhands"
POD = MANIFESTS / "images" / "agent-server-pod.yaml"
POLICIES = MANIFESTS / "policies"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_http_ready_detects_a_serving_endpoint() -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _http_ready(port) is True
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_ready_false_when_nothing_listens() -> None:
    assert _http_ready(_free_port()) is False


def test_pod_pins_agent_server_image_and_hardens_the_container() -> None:
    pod = _load(POD)
    container = pod["spec"]["containers"][0]
    assert container["image"].startswith("ghcr.io/openhands/agent-server:1.56.0-python@sha256:")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["ports"][0]["containerPort"] == 8000
    assert pod["metadata"]["labels"]["app"] == "openhands-sandbox"
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["runAsUser"] == 10001
    assert container["readinessProbe"]["exec"]["command"][-1].endswith(":8000/health")


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
    assert built["spec"]["containers"][0]["securityContext"]["runAsUser"] == 10001
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


def test_pod_without_workspace_binding_writes_only_to_scratch_volumes() -> None:
    built = build_pod_manifest(SandboxSpec(execution_id="exec-1", image="img"))
    container = built["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {volume["name"] for volume in built["spec"]["volumes"]} == {"agent-tmp", "agent-home"}
    assert {mount["mountPath"] for mount in container["volumeMounts"]} == {"/tmp", "/home/sandbox"}
    assert container["workingDir"] == "/home/sandbox"
    assert {"name": "HOME", "value": "/home/sandbox"} in container["env"]


def test_pod_builder_mounts_match_the_declarative_manifest() -> None:
    pod = _load(POD)
    built = build_pod_manifest(SandboxSpec(execution_id="exec-1", image="img"))
    assert built["spec"]["volumes"] == pod["spec"]["volumes"]
    assert (
        built["spec"]["containers"][0]["volumeMounts"]
        == pod["spec"]["containers"][0]["volumeMounts"]
    )


def test_pod_binds_an_ephemeral_workspace_volume() -> None:
    built = build_pod_manifest(
        SandboxSpec(
            execution_id="exec-1",
            image="img",
            workspace=WorkspaceBinding(),
        )
    )
    container = built["spec"]["containers"][0]
    volume = built["spec"]["volumes"][-1]
    mount = container["volumeMounts"][-1]
    assert volume["emptyDir"] == {}
    assert volume["name"] == mount["name"] == "agent-workspace"
    assert mount["mountPath"] == "/workspace"
    assert "subPath" not in mount
    assert container["workingDir"] == "/workspace"
    assert {"name": "WORKSPACE_BASE", "value": "/workspace"} in container["env"]


def test_pod_workspace_honours_a_custom_mount_path() -> None:
    built = build_pod_manifest(
        SandboxSpec(
            execution_id="exec-1",
            image="img",
            workspace=WorkspaceBinding(mount_path="/repo"),
        )
    )
    container = built["spec"]["containers"][0]
    volume = built["spec"]["volumes"][-1]
    assert volume["emptyDir"] == {}
    assert container["volumeMounts"][-1]["mountPath"] == "/repo"
    assert container["workingDir"] == "/repo"


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
            namespace = target.get("namespaceSelector", {}).get("matchLabels", {})
            pod_selector = target.get("podSelector", {}).get("matchLabels", {})
            selectors.append((namespace.get("kubernetes.io/metadata.name"), pod_selector))
    assert ("kube-system", {"k8s-app": "kube-dns"}) in selectors
    assert ("agentic-dev-platform", {"app.kubernetes.io/name": "litellm"}) in selectors
    assert (None, {"agentic-dev-platform/mediator": "true"}) in selectors


def test_security_group_policy_targets_the_sandbox_pods() -> None:
    policy = _load(POLICIES / "securitygrouppolicy.yaml")
    assert policy["kind"] == "SecurityGroupPolicy"
    assert policy["apiVersion"].startswith("vpcresources.k8s.aws/")
    assert policy["spec"]["podSelector"]["matchLabels"]["app"] == "openhands-sandbox"


def test_a_session_key_reaches_the_agent_server_environment() -> None:
    spec = SandboxSpec(execution_id="exec-1", image="agent-server:test", session_api_key="k" * 40)

    env = build_pod_manifest(spec)["spec"]["containers"][0]["env"]

    assert {"name": "SESSION_API_KEY", "value": "k" * 40} in env


def test_no_session_key_leaves_the_agent_server_on_loopback() -> None:
    env = build_pod_manifest(SandboxSpec(execution_id="exec-1", image="agent-server:test"))["spec"][
        "containers"
    ][0]["env"]

    assert all(variable["name"] != "SESSION_API_KEY" for variable in env)


def test_the_sandbox_pod_lands_on_the_tainted_node_pool() -> None:
    spec = build_pod_manifest(SandboxSpec(execution_id="exec-1", image="img", node_pool="sandbox"))[
        "spec"
    ]

    assert spec["nodeSelector"] == {NODE_POOL_LABEL: "sandbox"}
    assert spec["tolerations"] == [
        {"key": SANDBOX_TAINT_KEY, "operator": "Exists", "effect": "NoSchedule"}
    ]
    assert (
        "nodeSelector" not in build_pod_manifest(SandboxSpec(execution_id="e", image="i"))["spec"]
    )


def test_the_node_pool_label_and_taint_match_the_eks_node_groups() -> None:
    nodes = (REPO_ROOT / "infra/terraform/eks-cluster/nodes.tf").read_text(encoding="utf-8")

    assert f'node_pool_label = "{NODE_POOL_LABEL}"' in nodes
    assert f'key = "{SANDBOX_TAINT_KEY}"' in nodes
    assert 'effect = "NO_SCHEDULE"' in nodes
