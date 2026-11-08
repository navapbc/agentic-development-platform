from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
import yaml
from scripts import k8s_render
from scripts.cluster_env import compose_network, rewrite_address, rewrite_env
from scripts.k8s_cluster import exec_argv, one_off_argv, removable
from scripts.k8s_render import RenderInputs, render
from tests.support import hermetic_settings

REPOSITORY = Path(__file__).resolve().parents[2]

NETWORK = compose_network(
    {
        "services": {
            "postgres": {"ports": [{"target": 5432, "published": "5432"}]},
            "minio": {"ports": [{"target": 9000, "published": "9090"}]},
            "coordination-service": {"ports": [{"target": 8130, "published": "8130"}]},
            "repo-intelligence": {"ports": [{"target": 8120, "published": "8120"}]},
            "temporal": {"ports": [{"target": 7233, "published": "7233"}]},
            "redis": {"ports": [{"target": 6379}]},
        }
    }
)


def test_a_compose_address_becomes_the_published_port_on_the_pod_address() -> None:
    value, unreachable = rewrite_address(
        "postgresql+asyncpg://u:p@postgres:5432/db", NETWORK, "host.docker.internal"
    )

    assert value == "postgresql+asyncpg://u:p@host.docker.internal:5432/db"
    assert unreachable == set()


def test_a_remapped_port_follows_the_published_one() -> None:
    value, _ = rewrite_address("http://minio:9000", NETWORK, "host.docker.internal")

    assert value == "http://host.docker.internal:9090"


def test_litellm_keeps_its_service_name_and_opa_moves_to_loopback() -> None:
    assert rewrite_address("http://litellm:4000", NETWORK, "h")[0] == "http://litellm:4000"
    assert rewrite_address("http://opa:8181", NETWORK, "h")[0] == "http://127.0.0.1:8181"


def test_an_unpublished_compose_port_is_reported() -> None:
    value, unreachable = rewrite_address("redis:6379", NETWORK, "h")

    assert value == "redis:6379"
    assert unreachable == {"redis:6379"}


def test_workstation_settings_stay_on_the_workstation() -> None:
    rewritten = rewrite_env(
        {"AWS_PROFILE": "p", "KUBECONFIG": "k", "TEMPORAL_HOST": "temporal:7233", "EMPTY": None},
        NETWORK,
        "h",
    )

    assert rewritten.values == {"TEMPORAL_HOST": "h:7233"}


def test_a_multi_line_value_is_refused() -> None:
    with pytest.raises(ValueError, match="multi-line"):
        rewrite_env({"KEY": "a\nb"}, NETWORK, "h")


def _rendered(tmp_path: Path, **settings: object) -> list[dict]:
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    for directory in ("deploy", "openhands/policies", "config/policies", "config/platform"):
        shutil.copytree(REPOSITORY / directory, root / directory)
    config = root / "config" / "local"
    config.mkdir()
    for name in ("github.yaml", "jira.yaml", "integrations.yaml", "repositories.yaml"):
        (config / name).write_text((REPOSITORY / "config/local" / f"{name}.example").read_text())
    (config / "review-signing-key").write_text("secret-key\n")
    (config / "rebar-store-aws-config").write_text("endpoint_url = http://minio:9000\n")
    return render(
        RenderInputs(
            settings=hermetic_settings(**settings),
            env={
                "DATABASE_URL": "postgresql://u:p@postgres:5432/db",
                "AWS_PROFILE": "p",
                "MCP_GATEWAY_TOKEN": "gateway-token",
            },
            network=NETWORK,
            build_id="0.3.0.1",
            root=root,
        )
    )


def _named(documents: list[dict], kind: str, name: str) -> dict:
    return next(d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name)


def _pod_specs(documents: list[dict]) -> list[dict]:
    return [d["spec"]["template"]["spec"] for d in documents if d["kind"] == "Deployment"]


def test_no_pod_mounts_a_host_path(tmp_path: Path) -> None:
    volumes = [v for spec in _pod_specs(_rendered(tmp_path)) for v in spec.get("volumes", [])]

    assert volumes
    assert all("hostPath" not in volume for volume in volumes)


def test_the_workers_run_the_build_image_and_report_its_build_id(tmp_path: Path) -> None:
    documents = _rendered(tmp_path)

    for name in ("activity-worker", "review-worker"):
        container = _named(documents, "Deployment", name)["spec"]["template"]["spec"]["containers"][
            0
        ]
        assert container["image"] == "agentic-dev-platform-local:0.3.0.1"
        assert {"name": "AGENT_BUILD_ID", "value": "0.3.0.1"} in container["env"]
    opa = _named(documents, "Deployment", "activity-worker")["spec"]["template"]["spec"][
        "initContainers"
    ][0]
    assert opa["image"] == "agentic-dev-platform-opa:0.3.0.1"
    assert opa["restartPolicy"] == "Always"
    assert "--addr=127.0.0.1:8181" in opa["args"]


def test_no_service_exposes_opa(tmp_path: Path) -> None:
    services = [d for d in _rendered(tmp_path) if d["kind"] == "Service"]

    assert all(port["port"] != 8181 for s in services for port in s["spec"]["ports"])


def test_credentials_and_keys_travel_in_secrets_and_yaml_in_a_config_map(tmp_path: Path) -> None:
    documents = _rendered(tmp_path)

    config = _named(documents, "ConfigMap", "platform-config")["data"]
    secrets = _named(documents, "Secret", "platform-config-secrets")["data"]
    env = _named(documents, "Secret", "agentic-platform-env")["data"]
    assert "repositories.yaml" in config and "review-signing-key" not in config
    assert base64.b64decode(secrets["review-signing-key"]) == b"secret-key\n"
    store = base64.b64decode(secrets["rebar-store-aws-config"]).decode()
    assert store == "endpoint_url = http://host.docker.internal:9090\n"
    assert base64.b64decode(env["DATABASE_URL"]).decode().endswith("@host.docker.internal:5432/db")
    assert "AWS_PROFILE" not in env


def test_a_tunnel_provider_relays_bedrock_into_each_bedrock_client(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, bedrock_connectivity_provider="jumphost")

    for name in ("litellm", "review-worker"):
        spec = _named(documents, "Deployment", name)["spec"]["template"]["spec"]
        relay = next(c for c in spec["initContainers"] if c["name"] == "bedrock-relay")
        assert relay["args"][1] == "TCP:host.docker.internal:8444"
        assert spec["hostAliases"] == [
            {"ip": "127.0.0.1", "hostnames": ["bedrock-runtime.us-east-1.amazonaws.com"]}
        ]
    activity = _named(documents, "Deployment", "activity-worker")["spec"]["template"]["spec"]
    assert "hostAliases" not in activity


def test_the_direct_provider_runs_no_relay(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, bedrock_connectivity_provider="direct")

    for spec in _pod_specs(documents):
        names = [c["name"] for c in spec.get("initContainers", [])]
        assert "bedrock-relay" not in names


def test_configured_namespaces_move_every_resource_and_reference(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, k8s_namespace="platform", openhands_sandbox_namespace="sbx")

    namespaces = {d["metadata"].get("namespace") for d in documents} - {None}
    assert namespaces == {"platform", "sbx"}
    binding = _named(documents, "RoleBinding", "activity-worker-sandbox")
    assert binding["subjects"][0]["namespace"] == "platform"
    activity = _named(documents, "Deployment", "activity-worker")["spec"]["template"]["spec"]
    sandbox_url = next(
        e["value"]
        for e in activity["containers"][0]["env"]
        if e["name"] == "SANDBOX_LITELLM_BASE_URL"
    )
    assert sandbox_url == "http://litellm.platform.svc.cluster.local:4000"
    policy = _named(documents, "NetworkPolicy", "gate-engine-egress")
    assert "platform" in json.dumps(policy["spec"])
    assert any(d["kind"] == "Namespace" and d["metadata"]["name"] == "sbx" for d in documents)
    assert yaml.safe_load_all(k8s_render.dump(documents))


def test_a_delete_keeps_the_namespaces_and_the_volume_unless_asked() -> None:
    documents = [
        {"kind": "Namespace"},
        {"kind": "PersistentVolumeClaim"},
        {"kind": "Deployment"},
    ]

    assert [d["kind"] for d in removable(documents, volumes=False)] == ["Deployment"]
    assert [d["kind"] for d in removable(documents, volumes=True)] == [
        "PersistentVolumeClaim",
        "Deployment",
    ]


def test_scripted_tasks_run_in_the_activity_worker_of_the_configured_context() -> None:
    settings = hermetic_settings(k8s_context="ctx", k8s_namespace="ns")

    assert exec_argv(settings, ["python", "-m", "x"], tty=False) == [
        "kubectl",
        "--context",
        "ctx",
        "--namespace",
        "ns",
        "exec",
        "-i",
        "deploy/activity-worker",
        "-c",
        "activity-worker",
        "--",
        "python",
        "-m",
        "x",
    ]


def test_a_one_off_pod_copies_the_worker_template_without_its_probes() -> None:
    deployment = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "activity-worker",
                            "image": "img:1",
                            "args": ["python", "-m", "worker"],
                            "livenessProbe": {},
                            "startupProbe": {},
                        }
                    ],
                    "volumes": [{"name": "workspaces"}],
                }
            }
        }
    }

    argv = one_off_argv(hermetic_settings(), deployment, ["python", "-m", "job"], "job")
    overrides = json.loads(next(a for a in argv if a.startswith("--overrides=")).split("=", 1)[1])
    container = overrides["spec"]["containers"][0]

    assert container["name"] == "job"
    assert container["args"] == ["python", "-m", "job"]
    assert "command" not in container
    assert "livenessProbe" not in container and "startupProbe" not in container
    assert overrides["spec"]["restartPolicy"] == "Never"
    assert overrides["spec"]["volumes"] == [{"name": "workspaces"}]


def test_a_config_change_changes_every_pod_template_and_a_rerender_does_not(
    tmp_path: Path,
) -> None:
    def hashes(documents: list[dict]) -> dict[str, str]:
        return {
            d["metadata"]["name"]: d["spec"]["template"]["metadata"]["annotations"][
                k8s_render.CONFIG_HASH
            ]
            for d in documents
            if d["kind"] == "Deployment"
        }

    first = hashes(_rendered(tmp_path / "a"))
    again = hashes(_rendered(tmp_path / "b"))
    changed = hashes(_rendered(tmp_path / "c", temporal_namespace="other"))

    assert first == again
    assert len(first) == 5
    assert all(changed[name] != first[name] for name in first)


def test_the_gateway_token_reaches_the_workload_secret(tmp_path: Path) -> None:
    workload = _named(_rendered(tmp_path), "Secret", "agentic-platform")["data"]

    assert base64.b64decode(workload["mcp_gateway_token"]) == b"gateway-token"
