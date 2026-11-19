from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from scripts import k8s_render
from scripts.cluster_env import (
    compose_network,
    litellm_database_url,
    qualify_in_cluster_services,
    rewrite_address,
    rewrite_env,
)
from scripts.k8s_cluster import (
    apply,
    delete_retired,
    deployments,
    exec_argv,
    one_off_argv,
    removable,
)
from scripts.k8s_render import RenderInputs, render
from services import process
from services.repository_intelligence.tenant_tokens import derive_token
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

CLAIMS_REPOSITORIES = """config_version: 1
repositories:
  claims-web:
    backend: local
    path: /src/claims-web
"""
BETA_REPOSITORIES = """config_version: 1
repositories:
  beta-web:
    backend: local
    path: /src/beta-web
    profile: node-react
"""
ADP_TICKETS = """config_version: 1
backend:
  repo_root: /tenants/adp/rebar-tickets
store:
  bucket: rebar-tickets
  prefix: tickets/
"""


def _beta_tickets(repo_root: str = "/tenants/beta/rebar-tickets", prefix: str = "beta/tickets/"):
    return ADP_TICKETS.replace("/tenants/adp/rebar-tickets", repo_root).replace(
        "prefix: tickets/", f"prefix: {prefix}"
    )


def _beta_files(**extra: str) -> dict[str, str]:
    return {
        "tickets.yaml": ADP_TICKETS,
        "authorship-signing-key": "adp-signing-key\n",
        "tenants/beta/repositories.yaml": BETA_REPOSITORIES,
        "tenants/beta/tickets.yaml": _beta_tickets(),
        "tenants/beta.env": "GITHUB_TOKEN=beta-token\nREBAR_PROJECT=BETA\n"
        "EVIDENCE_URL=http://minio:9000/evidence\nMCP_GATEWAY_TOKEN=beta-gateway-token\n",
        **extra,
    }


ADP_ENV = {"GITHUB_TOKEN": "adp-token", "TICKET_BACKEND": "rebar", "SLACK_WEBHOOK_URL": "adp-hook"}
TOKEN_KEY = "workstation-token-key"


def test_a_compose_address_becomes_the_published_port_on_the_pod_address() -> None:
    value, unreachable = rewrite_address(
        "postgresql+asyncpg://u:p@postgres:5432/db", NETWORK, "host.docker.internal"
    )

    assert value == "postgresql+asyncpg://u:p@host.docker.internal:5432/db"
    assert unreachable == set()


def test_a_remapped_port_follows_the_published_one() -> None:
    value, _ = rewrite_address("http://minio:9000", NETWORK, "host.docker.internal")

    assert value == "http://host.docker.internal:9090"


def test_an_in_cluster_service_is_qualified_with_its_namespace_and_others_are_kept() -> None:
    assert qualify_in_cluster_services("http://litellm:4000/v1", "platform") == (
        "http://litellm.platform.svc.cluster.local:4000/v1"
    )
    assert qualify_in_cluster_services("http://minio:9000", "platform") == "http://minio:9000"


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


def _rendered(
    tmp_path: Path,
    files: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    **settings: object,
) -> list[dict]:
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    for directory in ("deploy", "openhands/policies", "config/policies", "config/platform"):
        shutil.copytree(REPOSITORY / directory, root / directory)
    config = root / "config" / "local"
    config.mkdir()
    for name in (
        "github.yaml",
        "jira.yaml",
        "integrations.yaml",
        "repositories.yaml",
        "image_policy.yaml",
    ):
        (config / name).write_text((REPOSITORY / "config/local" / f"{name}.example").read_text())
    (config / "review-signing-key").write_text("secret-key\n")
    (config / "rebar-store-aws-config").write_text("endpoint_url = http://minio:9000\n")
    for name, text in (files or {}).items():
        (config / name).parent.mkdir(parents=True, exist_ok=True)
        (config / name).write_text(text)
    return render(
        RenderInputs(
            settings=hermetic_settings(**{"tenants": "adp", **settings}),
            env={
                "DATABASE_URL": "postgresql://u:p@postgres:5432/db",
                "AWS_PROFILE": "p",
                "MCP_GATEWAY_TOKEN": "gateway-token",
                **(env or {}),
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
    opa = next(
        c
        for c in _named(documents, "Deployment", "activity-worker")["spec"]["template"]["spec"][
            "initContainers"
        ]
        if c["name"] == "opa"
    )
    assert opa["image"] == "agentic-dev-platform-opa:0.3.0.1"
    assert opa["restartPolicy"] == "Always"
    assert "--addr=127.0.0.1:8181" in opa["args"]


def test_litellm_runs_the_image_built_from_the_litellm_dockerfile(tmp_path: Path) -> None:
    litellm = _named(_rendered(tmp_path), "Deployment", "litellm")

    container = litellm["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "agentic-dev-platform-litellm:0.3.0.1"


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
    assert namespaces == {"platform", "tenant-adp", "tenant-adp-sandbox"}
    binding = _named(documents, "RoleBinding", "activity-worker-sandbox")
    assert binding["metadata"]["namespace"] == "tenant-adp-sandbox"
    assert binding["subjects"][0]["namespace"] == "tenant-adp"
    activity = _named(documents, "Deployment", "activity-worker")["spec"]["template"]["spec"]
    sandbox_url = next(
        e["value"]
        for e in activity["containers"][0]["env"]
        if e["name"] == "SANDBOX_LITELLM_BASE_URL"
    )
    assert sandbox_url == "http://litellm.platform.svc.cluster.local:4000"
    policy = _named(documents, "NetworkPolicy", "gate-engine-egress")
    assert "platform" in json.dumps(policy["spec"])
    assert not any(d["kind"] == "Namespace" and d["metadata"]["name"] == "sbx" for d in documents)
    assert yaml.safe_load_all(k8s_render.dump(documents))


def test_each_tenant_writes_its_own_host_directory_and_the_review_worker_reads_them_all(
    tmp_path: Path,
) -> None:
    files = {
        "tenants/claims/repositories.yaml": CLAIMS_REPOSITORIES,
        "tenants/claims.env": "MCP_GATEWAY_TOKEN=claims-gateway-token\n",
    }
    documents = _rendered(tmp_path, files, tenants="adp,claims")

    for tenant in ("adp", "claims"):
        worker = next(
            d
            for d in documents
            if d["kind"] == "Deployment" and d["metadata"]["namespace"] == f"tenant-{tenant}"
        )
        spec = worker["spec"]["template"]["spec"]
        owner = spec["initContainers"][0]
        assert owner["command"] == ["chown", "10001:10001", f"/tenants/{tenant}"]
        assert owner["securityContext"]["capabilities"] == {"drop": ["ALL"], "add": ["CHOWN"]}
        claim = next(
            d
            for d in documents
            if d["kind"] == "PersistentVolumeClaim"
            and d["metadata"]["namespace"] == f"tenant-{tenant}"
        )
        volume = _named(documents, "PersistentVolume", claim["spec"]["volumeName"])
        assert volume["spec"]["hostPath"]["path"] == f"{k8s_render.TENANTS_HOST_ROOT}/{tenant}"
    review = _named(documents, "Deployment", "review-worker")["spec"]["template"]["spec"]
    mounts = {m["name"]: m for m in review["containers"][0]["volumeMounts"]}
    assert mounts["tenants"]["mountPath"] == "/tenants" and mounts["tenants"]["readOnly"]
    claim = next(
        d
        for d in documents
        if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "tenants"
    )
    volume = _named(documents, "PersistentVolume", claim["spec"]["volumeName"])
    assert volume["spec"]["hostPath"]["path"] == k8s_render.TENANTS_HOST_ROOT


def test_the_local_render_refuses_a_workstation_without_a_tenant(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="TENANTS or TENANT"):
        _rendered(tmp_path, tenants="")


def test_a_delete_keeps_the_namespaces_and_the_volume_unless_asked() -> None:
    documents = [
        {"kind": "Namespace"},
        {"kind": "PersistentVolume"},
        {"kind": "PersistentVolumeClaim"},
        {"kind": "Deployment"},
    ]

    assert [d["kind"] for d in removable(documents, volumes=False)] == ["Deployment"]
    assert [d["kind"] for d in removable(documents, volumes=True)] == [
        "PersistentVolume",
        "PersistentVolumeClaim",
        "Deployment",
    ]


def test_scripted_tasks_run_in_the_activity_worker_of_the_configured_context() -> None:
    settings = hermetic_settings(k8s_context="ctx", k8s_namespace="ns", tenant="adp")

    assert exec_argv(settings, ["python", "-m", "x"], tty=False) == [
        "kubectl",
        "--context",
        "ctx",
        "--namespace",
        "tenant-adp",
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

    argv = one_off_argv(hermetic_settings(tenant="adp"), deployment, ["python", "-m", "job"], "job")
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


def test_the_platform_workload_secret_holds_no_gateway_token(tmp_path: Path) -> None:
    workload = _named(_rendered(tmp_path), "Secret", "agentic-platform")["data"]

    assert "mcp_gateway_token" not in workload


def test_litellm_reads_its_own_database_on_the_compose_postgres(tmp_path: Path) -> None:
    documents = _rendered(tmp_path)
    workload = _named(documents, "Secret", "agentic-platform")["data"]
    platform = base64.b64decode(workload["database_url"]).decode()
    litellm = base64.b64decode(workload["litellm_database_url"]).decode()
    container = _named(documents, "Deployment", "litellm")["spec"]["template"]["spec"][
        "containers"
    ][0]
    env = {e["name"]: e for e in container["env"]}

    assert urlsplit(litellm).scheme == "postgresql"
    assert urlsplit(litellm).netloc == urlsplit(platform).netloc
    assert litellm.endswith("@host.docker.internal:5432/litellm")
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "agentic-platform",
        "key": "litellm_database_url",
    }


def test_the_litellm_database_url_drops_the_driver_and_the_platform_query() -> None:
    assert litellm_database_url("") == ""
    assert (
        litellm_database_url("postgresql+asyncpg://u:p@h:5432/agent_platform?ssl=disable")
        == "postgresql://u:p@h:5432/litellm"
    )


def test_each_tenant_worker_rolls_in_its_own_namespace() -> None:
    settings = hermetic_settings(k8s_namespace="ns", tenants="adp,claims")

    assert ("tenant-adp", "activity-worker") in deployments(settings)
    assert ("tenant-claims", "activity-worker") in deployments(settings)
    assert ("ns", "activity-worker") not in deployments(settings)
    assert ("tenant-adp", "mcp-gateway") in deployments(settings)
    assert ("tenant-claims", "mcp-gateway") in deployments(settings)
    assert ("ns", "mcp-gateway") not in deployments(settings)


def test_the_retired_objects_are_deleted_from_the_configured_namespaces() -> None:
    calls: list[list[str]] = []
    settings = hermetic_settings(k8s_namespace="ns", openhands_sandbox_namespace="sbx")

    def run(argv, **_):
        calls.append(argv)
        missing = "secretproviderclass" in argv
        return process.CompletedProcess(
            argv,
            1 if missing else 0,
            "",
            'error: the server doesn\'t have a resource type "secretproviderclass"'
            if missing
            else "",
        )

    delete_retired(settings, run)

    assert ["--namespace", "ns", "delete", "deployment", "activity-worker"] in [
        c[3:8] for c in calls
    ]
    assert ["delete", "namespace", "sbx", "--ignore-not-found"] in [c[3:7] for c in calls]


def _in(documents: list[dict], kind: str, namespace: str, name: str) -> dict | None:
    return next(
        (
            d
            for d in documents
            if d["kind"] == kind
            and d["metadata"].get("namespace") == namespace
            and d["metadata"]["name"] == name
        ),
        None,
    )


def _secret_values(documents: list[dict], namespace: str, name: str) -> dict[str, str]:
    found = _in(documents, "Secret", namespace, name)
    assert found is not None
    return {key: base64.b64decode(value).decode() for key, value in found["data"].items()}


def _worker(documents: list[dict], tenant: str) -> dict:
    found = _in(documents, "Deployment", f"tenant-{tenant}", "activity-worker")
    assert found is not None
    return found


def _gateway(documents: list[dict], tenant: str) -> dict:
    found = _in(documents, "Deployment", f"tenant-{tenant}", "mcp-gateway")
    assert found is not None
    return found


def _worker_env(documents: list[dict], tenant: str) -> dict[str, str]:
    container = _worker(documents, tenant)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


def _bundle_sources(documents: list[dict], tenant: str) -> list[dict]:
    volumes = _worker(documents, tenant)["spec"]["template"]["spec"]["volumes"]
    config = next(v for v in volumes if v["name"] == k8s_render.CONFIG_VOLUME)
    return [
        source["configMap"]
        for source in config["projected"]["sources"]
        if source.get("configMap", {}).get("name") == k8s_render.TENANT_BUNDLE
    ]


def test_a_second_tenant_gets_its_own_namespaces_account_queue_and_workspace(
    tmp_path: Path,
) -> None:
    from services.orchestration.task_queues import run_task_queue, tenant_schedule_id

    documents = _rendered(tmp_path, _beta_files(), ADP_ENV, tenants="adp,beta", tenant="adp")

    names = {d["metadata"]["name"] for d in documents if d["kind"] == "Namespace"}
    assert {"tenant-adp", "tenant-adp-sandbox", "tenant-beta", "tenant-beta-sandbox"} <= names
    assert _in(documents, "ServiceAccount", "tenant-beta", "activity-worker") is not None
    binding = _in(documents, "RoleBinding", "tenant-beta-sandbox", "activity-worker-sandbox")
    assert binding is not None and binding["subjects"][0]["namespace"] == "tenant-beta"
    env = _worker_env(documents, "beta")
    assert env["TENANT"] == "beta"
    assert env["OPENHANDS_SANDBOX_NAMESPACE"] == "tenant-beta-sandbox"
    assert env["REPOSITORY_ROOT"] == "/tenants/beta/repositories"
    assert env["EXECUTION_WORKSPACE_ROOT"] == "/tenants/beta/workspaces"
    beta = hermetic_settings(tenant=env["TENANT"])
    assert run_task_queue(beta) == "agent-platform-beta"
    assert tenant_schedule_id("rebar-intake", beta) == "rebar-intake-beta"
    assert tenant_schedule_id(beta.reap_schedule_id, beta) == "orphan-reaper-beta"
    claim = _in(documents, "PersistentVolumeClaim", "tenant-beta", "workspaces")
    assert claim is not None
    volume = _named(documents, "PersistentVolume", claim["spec"]["volumeName"])
    assert volume["spec"]["hostPath"]["path"] == f"{k8s_render.TENANTS_HOST_ROOT}/beta"


def test_each_tenant_mounts_only_its_own_bundle(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, _beta_files(), ADP_ENV, tenants="adp,beta", tenant="adp")

    bundle = _in(documents, "ConfigMap", "tenant-beta", k8s_render.TENANT_BUNDLE)
    assert bundle is not None
    assert set(bundle["data"]) == {"repositories.yaml", "tickets.yaml"}
    assert "beta-web" in bundle["data"]["repositories.yaml"]
    assert _bundle_sources(documents, "beta") == [
        {
            "name": k8s_render.TENANT_BUNDLE,
            "items": [
                {"key": "repositories.yaml", "path": "tenants/beta/repositories.yaml"},
                {"key": "tickets.yaml", "path": "tenants/beta/tickets.yaml"},
            ],
        }
    ]
    assert _in(documents, "ConfigMap", "tenant-adp", k8s_render.TENANT_BUNDLE) is None
    assert _bundle_sources(documents, "adp") == []


def test_no_tenant_receives_another_tenants_settings_or_signing_key(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, _beta_files(), ADP_ENV, tenants="adp,beta", tenant="adp")

    adp = _secret_values(documents, "tenant-adp", k8s_render.PLATFORM_ENV)
    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert adp["GITHUB_TOKEN"] == "adp-token" and adp["SLACK_WEBHOOK_URL"] == "adp-hook"
    assert "REBAR_PROJECT" not in adp and "beta-token" not in adp.values()
    assert beta["GITHUB_TOKEN"] == "beta-token" and beta["REBAR_PROJECT"] == "BETA"
    assert "SLACK_WEBHOOK_URL" not in beta and "adp-token" not in beta.values()
    assert beta["TICKET_BACKEND"] == "rebar"
    assert beta["DATABASE_URL"].endswith("@host.docker.internal:5432/db")
    assert beta["EVIDENCE_URL"] == "http://host.docker.internal:9090/evidence"
    assert "AWS_PROFILE" not in beta
    adp_files = _secret_values(documents, "tenant-adp", k8s_render.CONFIG_SECRETS)
    beta_files = _secret_values(documents, "tenant-beta", k8s_render.CONFIG_SECRETS)
    assert adp_files["authorship-signing-key"] == "adp-signing-key\n"
    assert "authorship-signing-key" not in beta_files
    assert "rebar-store-aws-config" in beta_files
    assert "review-signing-key" not in adp_files and "review-signing-key" not in beta_files
    review_files = _secret_values(documents, "agentic-dev-platform", k8s_render.CONFIG_SECRETS)
    assert review_files["review-signing-key"] == "secret-key\n"
    annotations = _worker(documents, "beta")["spec"]["template"]["metadata"]["annotations"]
    assert k8s_render.TENANT_SETTINGS_HASH in annotations


def test_a_tenant_without_a_settings_file_other_than_the_home_tenant_gets_no_owned_settings(
    tmp_path: Path,
) -> None:
    files = _beta_files(**{"tenants/beta.env": "MCP_GATEWAY_TOKEN=beta-gateway-token\n"})

    documents = _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")

    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert set(beta) & k8s_render.TENANT_OWNED_SETTINGS == {"MCP_GATEWAY_TOKEN"}
    assert beta["TICKET_BACKEND"] == "rebar"
    assert _secret_values(documents, "tenant-adp", k8s_render.PLATFORM_ENV)["GITHUB_TOKEN"] == (
        "adp-token"
    )


def test_the_home_tenant_without_tenant_files_renders_as_before_with_its_own_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"tickets.yaml": ADP_TICKETS, "authorship-signing-key": "adp-signing-key\n"}
    env = {**ADP_ENV, "REPO_INTELLIGENCE_TOKEN_KEY": TOKEN_KEY}
    localized = _rendered(tmp_path / "a", files, env, tenant="adp")
    monkeypatch.setattr(k8s_render, "localize_tenants", lambda documents, inputs, names: documents)
    before = _rendered(tmp_path / "b", files, env, tenant="adp")
    adp_files = _in(before, "Secret", "tenant-adp", k8s_render.CONFIG_SECRETS)
    assert adp_files is not None
    del adp_files["data"]["review-signing-key"]
    adp_env = _in(before, "Secret", "tenant-adp", k8s_render.PLATFORM_ENV)
    assert adp_env is not None
    assert "REPO_INTELLIGENCE_TOKEN_KEY" not in adp_env["data"]
    adp_env["data"]["REPO_INTELLIGENCE_TOKEN"] = base64.b64encode(
        derive_token(TOKEN_KEY, "adp").encode()
    ).decode()
    annotations = _worker(localized, "adp")["spec"]["template"]["metadata"]["annotations"]
    assert annotations.pop(k8s_render.TENANT_SETTINGS_HASH)
    gateway = _gateway(localized, "adp")["spec"]["template"]["metadata"]["annotations"]
    assert gateway.pop(k8s_render.TENANT_SETTINGS_HASH)

    assert localized == before


def test_each_tenant_worker_receives_its_own_token_and_no_pod_the_key(tmp_path: Path) -> None:
    env = {**ADP_ENV, "REPO_INTELLIGENCE_TOKEN_KEY": TOKEN_KEY}
    documents = _rendered(tmp_path, _beta_files(), env, tenants="adp,beta", tenant="adp")

    adp = _secret_values(documents, "tenant-adp", k8s_render.PLATFORM_ENV)
    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert adp["REPO_INTELLIGENCE_TOKEN"] == derive_token(TOKEN_KEY, "adp")
    assert beta["REPO_INTELLIGENCE_TOKEN"] == derive_token(TOKEN_KEY, "beta")
    assert adp["REPO_INTELLIGENCE_TOKEN"] != beta["REPO_INTELLIGENCE_TOKEN"]
    assert "REPO_INTELLIGENCE_TOKEN_KEY" not in adp and "REPO_INTELLIGENCE_TOKEN_KEY" not in beta
    assert TOKEN_KEY not in adp.values() and TOKEN_KEY not in beta.values()
    platform = _secret_values(documents, "agentic-dev-platform", k8s_render.PLATFORM_ENV)
    assert "REPO_INTELLIGENCE_TOKEN_KEY" not in platform
    assert TOKEN_KEY not in platform.values()
    assert "REPO_INTELLIGENCE_TOKEN" not in platform


def test_a_tenant_token_set_in_env_replaces_the_derived_one_for_that_tenant_only(
    tmp_path: Path,
) -> None:
    env = {
        **ADP_ENV,
        "REPO_INTELLIGENCE_TOKEN_KEY": TOKEN_KEY,
        "REPO_INTELLIGENCE_TOKEN_BETA": "beta-ris-token",
    }
    documents = _rendered(tmp_path, _beta_files(), env, tenants="adp,beta", tenant="adp")

    adp = _secret_values(documents, "tenant-adp", k8s_render.PLATFORM_ENV)
    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert beta["REPO_INTELLIGENCE_TOKEN"] == "beta-ris-token"
    assert adp["REPO_INTELLIGENCE_TOKEN"] == derive_token(TOKEN_KEY, "adp")
    assert "beta-ris-token" not in adp.values()
    assert "REPO_INTELLIGENCE_TOKEN_BETA" not in beta


def test_a_new_token_key_restarts_every_tenant_worker(tmp_path: Path) -> None:
    def settings_hashes(key: str, directory: str) -> dict[str, str]:
        env = {**ADP_ENV, "REPO_INTELLIGENCE_TOKEN_KEY": key}
        documents = _rendered(
            tmp_path / directory, _beta_files(), env, tenants="adp,beta", tenant="adp"
        )
        return {
            tenant: _worker(documents, tenant)["spec"]["template"]["metadata"]["annotations"][
                k8s_render.TENANT_SETTINGS_HASH
            ]
            for tenant in ("adp", "beta")
        }

    first = settings_hashes(TOKEN_KEY, "a")

    assert first == settings_hashes(TOKEN_KEY, "b")
    assert all(hashed != first[tenant] for tenant, hashed in settings_hashes("other", "c").items())


@pytest.mark.parametrize(
    "setting",
    ["REPO_INTELLIGENCE_TOKEN", "REPO_INTELLIGENCE_TOKEN_KEY", "REPO_INTELLIGENCE_TOKEN_ADP"],
)
def test_a_tenant_settings_file_may_not_set_a_repository_intelligence_token(
    tmp_path: Path, setting: str
) -> None:
    files = _beta_files(**{"tenants/beta.env": f"{setting}=chosen-by-beta\n"})

    with pytest.raises(ValueError, match=f"tenants/beta.env sets {setting}\\. The render"):
        _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")


def test_a_second_tenant_must_name_its_own_repositories(tmp_path: Path) -> None:
    files = _beta_files()
    del files["tenants/beta/repositories.yaml"]

    with pytest.raises(ValueError, match="tenant beta names no repositories"):
        _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")


@pytest.mark.parametrize(
    ("beta_tickets", "reason"),
    [
        (_beta_tickets(prefix="tickets/"), "overlapping locations in bucket rebar-tickets"),
        (_beta_tickets(prefix="tickets/beta/"), "overlapping locations in bucket rebar-tickets"),
        (_beta_tickets(repo_root="/tenants/adp/rebar-tickets"), "outside its workspace"),
        (None, "outside its workspace /tenants/beta"),
    ],
)
def test_each_rebar_tenant_keeps_its_own_ticket_store(
    tmp_path: Path, beta_tickets: str | None, reason: str
) -> None:
    files = _beta_files()
    if beta_tickets is None:
        del files["tenants/beta/tickets.yaml"]
    else:
        files["tenants/beta/tickets.yaml"] = beta_tickets

    with pytest.raises(ValueError, match=reason):
        _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")


def test_a_jira_tenant_needs_no_ticket_store(tmp_path: Path) -> None:
    files = _beta_files(
        **{"tenants/beta.env": "TICKET_BACKEND=jira\nMCP_GATEWAY_TOKEN=beta-gateway-token\n"}
    )
    del files["tenants/beta/tickets.yaml"]

    documents = _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")

    assert _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)["TICKET_BACKEND"] == (
        "jira"
    )


def test_two_tenants_may_not_share_an_intake_schedule(tmp_path: Path) -> None:
    files = _beta_files(**{"tenants/beta.env": "INTAKE_SCHEDULE_ID=intake\n"})

    with pytest.raises(ValueError, match="share INTAKE_SCHEDULE_ID intake"):
        _rendered(
            tmp_path,
            files,
            {**ADP_ENV, "INTAKE_SCHEDULE_ID": "intake"},
            tenants="adp,beta",
            tenant="adp",
        )


def test_an_invalid_tenant_setting_refuses_the_render(tmp_path: Path) -> None:
    files = _beta_files(**{"tenants/beta.env": "LOG_LEVEL=loud\n"})

    with pytest.raises(ValueError, match="LOG_LEVEL"):
        _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")


def test_the_second_tenant_examples_render(tmp_path: Path) -> None:
    examples = REPOSITORY / "config/local/tenants"
    files = {
        f"tenants/beta/{path.name}": path.read_text()
        for path in sorted((examples / "beta.example").iterdir())
    }
    files["tenants/beta.env"] = (examples / "beta.env.example").read_text()
    files["tickets.yaml"] = ADP_TICKETS

    documents = _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")

    bundle = _in(documents, "ConfigMap", "tenant-beta", k8s_render.TENANT_BUNDLE)
    assert bundle is not None
    assert set(bundle["data"]) == {"checks.yaml", "gates.yaml", "repositories.yaml", "tickets.yaml"}
    assert _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)["TICKET_BACKEND"] == (
        "rebar"
    )


def test_each_tenant_runs_its_own_gateway_with_only_its_own_settings(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, _beta_files(), ADP_ENV, tenants="adp,beta", tenant="adp")

    assert not [
        d
        for d in documents
        if d["metadata"]["name"] == "mcp-gateway"
        and d["metadata"].get("namespace") == "agentic-dev-platform"
    ]
    for tenant in ("adp", "beta"):
        namespace = f"tenant-{tenant}"
        gateway = _gateway(documents, tenant)
        spec = gateway["spec"]["template"]["spec"]
        container = spec["containers"][0]
        values = {e["name"]: e["value"] for e in container["env"] if "value" in e}
        references = [e["valueFrom"] for e in container["env"] if "valueFrom" in e] + container[
            "envFrom"
        ]

        assert spec["serviceAccountName"] == "activity-worker"
        assert values["TENANT"] == tenant
        assert container["envFrom"] == [{"secretRef": {"name": k8s_render.PLATFORM_ENV}}]
        assert all(
            _in(documents, kind, namespace, ref.get("name", ""))
            for reference in references
            for kind, ref in (
                ("Secret", reference.get("secretKeyRef") or reference.get("secretRef") or {}),
                (
                    "ConfigMap",
                    reference.get("configMapKeyRef") or reference.get("configMapRef") or {},
                ),
            )
            if ref
        )
        assert _in(documents, "Service", namespace, "mcp-gateway") is not None
    adp = _secret_values(documents, "tenant-adp", k8s_render.PLATFORM_ENV)
    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert adp["MCP_GATEWAY_TOKEN"] == "gateway-token"
    assert beta["MCP_GATEWAY_TOKEN"] == "beta-gateway-token"
    assert "gateway-token" not in beta.values() and "adp-token" not in beta.values()
    assert "beta-gateway-token" not in adp.values() and "beta-token" not in adp.values()


def test_each_tenant_gateway_reads_only_its_own_bundle(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, _beta_files(), ADP_ENV, tenants="adp,beta", tenant="adp")

    def sources(tenant: str) -> list[dict]:
        volumes = _gateway(documents, tenant)["spec"]["template"]["spec"]["volumes"]
        config = next(v for v in volumes if v["name"] == k8s_render.CONFIG_VOLUME)
        return [
            source["configMap"]
            for source in config["projected"]["sources"]
            if source.get("configMap", {}).get("name") == k8s_render.TENANT_BUNDLE
        ]

    assert sources("beta") == _bundle_sources(documents, "beta")
    assert sources("beta")[0]["items"][0]["path"].startswith("tenants/beta/")
    assert sources("adp") == []


@pytest.mark.parametrize(
    ("beta_env", "env", "source"),
    [
        ("GITHUB_TOKEN=beta-token\n", {}, "config/local/tenants/beta.env"),
        (None, {}, "config/local/tenants/beta.env"),
        ("MCP_GATEWAY_TOKEN=beta-gateway-token\n", {"MCP_GATEWAY_TOKEN": ""}, ".env"),
    ],
)
def test_a_tenant_without_its_own_gateway_token_is_refused(
    tmp_path: Path, beta_env: str | None, env: dict[str, str], source: str
) -> None:
    files = _beta_files()
    if beta_env is None:
        del files["tenants/beta.env"]
    else:
        files["tenants/beta.env"] = beta_env
    tenant = "adp" if source == ".env" else "beta"

    with pytest.raises(ValueError, match=f"tenant {tenant} has no MCP_GATEWAY_TOKEN.*{source}"):
        _rendered(tmp_path, files, {**ADP_ENV, **env}, tenants="adp,beta", tenant="adp")


def test_two_tenants_may_not_share_a_gateway_token(tmp_path: Path) -> None:
    files = _beta_files(**{"tenants/beta.env": "MCP_GATEWAY_TOKEN=gateway-token\n"})

    with pytest.raises(ValueError, match="tenants adp, beta share one MCP_GATEWAY_TOKEN"):
        _rendered(tmp_path, files, ADP_ENV, tenants="adp,beta", tenant="adp")


def test_a_new_gateway_token_restarts_only_that_tenants_gateway(tmp_path: Path) -> None:
    def hashes(token: str, directory: str) -> dict[str, str | None]:
        files = _beta_files(**{"tenants/beta.env": f"MCP_GATEWAY_TOKEN={token}\n"})
        documents = _rendered(
            tmp_path / directory, files, ADP_ENV, tenants="adp,beta", tenant="adp"
        )
        return {
            tenant: _gateway(documents, tenant)["spec"]["template"]["metadata"]["annotations"].get(
                k8s_render.TENANT_SETTINGS_HASH
            )
            for tenant in ("adp", "beta")
        }

    first, second = hashes("beta-one", "a"), hashes("beta-two", "b")

    assert first["adp"] == second["adp"]
    assert first["beta"] != second["beta"]


def test_apply_sends_each_workload_after_the_secrets_and_config_its_pods_read() -> None:
    sent: list[str] = []

    def run(argv, **kwargs):
        sent.append(kwargs["input"])
        return process.CompletedProcess(argv, 0, "", "")

    documents = [
        {"kind": "Namespace", "metadata": {"name": "platform"}},
        {"kind": "Deployment", "metadata": {"name": "inbound-webhook"}},
        {"kind": "Secret", "metadata": {"name": "inbound-webhook-tenants"}},
        {"kind": "ConfigMap", "metadata": {"name": "inbound-webhook-tenant-config"}},
    ]

    apply(hermetic_settings(), documents, run=run)

    kinds = [document["kind"] for document in yaml.safe_load_all(sent[0])]
    assert kinds == ["Namespace", "Secret", "ConfigMap", "Deployment"]


WEBHOOK_SECRETS = {
    "GITHUB_WEBHOOK_SECRET": "adp-github-signing",
    "JIRA_WEBHOOK_SECRET": "adp-jira-signing",
    "GITHUB_WEBHOOK_SECRET_ADP": "adp-github-own",
}


@pytest.mark.parametrize("tenants", ["adp", "adp,beta"])
def test_no_tenant_pod_receives_a_webhook_signing_secret(tmp_path: Path, tenants: str) -> None:
    files = _beta_files(
        **{
            "tenants/beta.env": "MCP_GATEWAY_TOKEN=beta-gateway-token\n"
            "GITHUB_WEBHOOK_SECRET=beta-github-signing\n"
        }
    )

    documents = _rendered(tmp_path, files, {**ADP_ENV, **WEBHOOK_SECRETS}, tenants=tenants)

    for tenant in tenants.split(","):
        values = _secret_values(documents, f"tenant-{tenant}", k8s_render.PLATFORM_ENV)
        assert not any("WEBHOOK_SECRET" in key for key in values)
        assert not {*WEBHOOK_SECRETS.values(), "beta-github-signing"} & set(values.values())
    platform = _secret_values(documents, "agentic-dev-platform", "agentic-platform")
    assert platform["github_webhook_secret"] == "adp-github-signing"
    assert platform["jira_webhook_secret"] == "adp-jira-signing"
    if "beta" in tenants:
        webhook = _secret_values(documents, "agentic-dev-platform", "inbound-webhook-tenants")
        assert webhook["GITHUB_WEBHOOK_SECRET_ADP"] == "adp-github-signing"
        assert webhook["GITHUB_WEBHOOK_SECRET_BETA"] == "beta-github-signing"


def test_a_tenant_worker_reaches_litellm_by_the_name_its_header_propagation_matches(
    tmp_path: Path,
) -> None:
    env = {**ADP_ENV, "REBAR_LLM_BASE_URL": "http://litellm:4000"}
    files = _beta_files(
        **{
            "tenants/beta.env": "MCP_GATEWAY_TOKEN=beta-gateway-token\n"
            "LITELLM_BASE_URL=http://litellm:4000\n"
        }
    )

    documents = _rendered(tmp_path, files, env, tenants="adp,beta", k8s_namespace="platform")

    for tenant in ("adp", "beta"):
        values = _secret_values(documents, f"tenant-{tenant}", k8s_render.PLATFORM_ENV)
        rebar = urlsplit(values["REBAR_LLM_BASE_URL"])
        assert rebar.hostname == "litellm.platform.svc.cluster.local"
        assert (
            rebar.hostname == urlsplit(_worker_env(documents, tenant)["LITELLM_BASE_URL"]).hostname
        )
    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert beta["LITELLM_BASE_URL"] == "http://litellm.platform.svc.cluster.local:4000"
    platform = _secret_values(documents, "platform", k8s_render.PLATFORM_ENV)
    assert platform["REBAR_LLM_BASE_URL"] == "http://litellm:4000"


SLACK_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-adp",
    "SLACK_APP_TOKEN": "xapp-adp",
    "ESCALATION_ACTION_KEY": "adp-action-key",
    "SLACK_SIGNING_SECRET": "adp-slack-signing",
}
BETA_SLACK = (
    "MCP_GATEWAY_TOKEN=beta-gateway-token\nSLACK_BOT_TOKEN=xoxb-beta\nSLACK_APP_TOKEN=xapp-beta\n"
    "ESCALATION_ACTION_KEY=beta-action-key\nSLACK_SIGNING_SECRET=beta-slack-signing\n"
)


def test_each_tenants_slack_credentials_reach_only_its_own_worker(tmp_path: Path) -> None:
    files = _beta_files(**{"tenants/beta.env": BETA_SLACK})

    documents = _rendered(
        tmp_path, files, {**ADP_ENV, **SLACK_ENV}, tenants="adp,beta", tenant="adp"
    )

    adp = _secret_values(documents, "tenant-adp", k8s_render.PLATFORM_ENV)
    beta = _secret_values(documents, "tenant-beta", k8s_render.PLATFORM_ENV)
    assert (adp["SLACK_BOT_TOKEN"], adp["SLACK_APP_TOKEN"], adp["ESCALATION_ACTION_KEY"]) == (
        "xoxb-adp",
        "xapp-adp",
        "adp-action-key",
    )
    assert (beta["SLACK_BOT_TOKEN"], beta["SLACK_APP_TOKEN"], beta["ESCALATION_ACTION_KEY"]) == (
        "xoxb-beta",
        "xapp-beta",
        "beta-action-key",
    )
    for pod_env in (adp, beta):
        assert not any(key.startswith("SLACK_SIGNING_SECRET") for key in pod_env)
    platform = _secret_values(documents, "agentic-dev-platform", k8s_render.PLATFORM_ENV)
    assert "SLACK_SIGNING_SECRET" not in platform


@pytest.mark.parametrize("setting", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ESCALATION_ACTION_KEY"])
def test_two_tenants_may_not_share_a_slack_credential(tmp_path: Path, setting: str) -> None:
    beta = BETA_SLACK.replace(f"{setting}=", f"{setting}=shared-{setting}\n#")
    env = {**ADP_ENV, **SLACK_ENV, setting: f"shared-{setting}"}
    files = _beta_files(**{"tenants/beta.env": beta})

    with pytest.raises(ValueError, match=f"share one {setting}"):
        _rendered(tmp_path, files, env, tenants="adp,beta", tenant="adp")
