"""Render the local cluster's manifests with the generated config, secrets, and build image.

The kustomize overlay holds the static shape. This adds what is derived from the workstation
at deploy time: the platform settings from `.env` with the compose addresses rewritten for the
pods, the operator config files, the promotion policies, the LiteLLM config, the image of the
current build, the Bedrock tunnel relay when the connectivity provider needs one, each tenant's
worker and sandbox namespaces, and the namespaces the settings name.

Each tenant's workspace is a hostPath directory on the node under TENANTS_HOST_ROOT, and the
review worker mounts that root read-only at /tenants, the layout the EFS access points give the
workers in AWS.

config/<environment>/tenants/ holds what belongs to one tenant. The files of <tenant>/ are that
tenant's config bundle, which only its activity worker and MCP gateway mount, at the path their
directory config source reads. <tenant>.env holds the settings and credentials only that
tenant's worker and gateway receive. `.env` belongs to the home tenant, TENANT or else the first
tenant TENANTS names, which without a settings file of its own receives every setting of `.env`.
Every other tenant gets none of the TENANT_OWNED_SETTINGS from `.env` and none of the values any
tenant's file sets, so one tenant's credentials never reach another tenant's pods.

Each tenant's worker receives its own repository intelligence token as REPO_INTELLIGENCE_TOKEN,
the token repository intelligence expects of that tenant, which it derives from
REPO_INTELLIGENCE_TOKEN_KEY unless `.env` sets REPO_INTELLIGENCE_TOKEN_<TENANT>. No cluster pod
receives the key, and no tenant's worker receives another tenant's token.

No tenant's worker receives a webhook signing secret, GITHUB_WEBHOOK_SECRET, JIRA_WEBHOOK_SECRET,
or a per-tenant variant of either, because only the inbound webhook reads them, from Secrets of
its own. A tenant's settings name an in-cluster service such as LiteLLM by its cluster DNS name in
the platform namespace, which the worker resolves from the tenant's namespace.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from services import process

from scripts.cluster_env import (
    WORKSTATION_SETTINGS,
    ComposeNetwork,
    endpoint_values,
    qualify_in_cluster_services,
    rewrite_address,
    rewrite_env,
    workload_secret_values,
)
from scripts.config import Settings

OVERLAY = Path("deploy/kubernetes/local")
PLATFORM_NAMESPACE = "agentic-dev-platform"
SANDBOX_NAMESPACE = "agent-sandbox"
PLATFORM_IMAGE = "agentic-dev-platform-local"
OPA_IMAGE = "agentic-dev-platform-opa"
LITELLM_IMAGE = "agentic-dev-platform-litellm"
LOCAL_TAG = "local"
BEDROCK_CLIENT = "agentic-dev-platform/bedrock-client"
CONFIG_HASH = "agentic-dev-platform/config-hash"
RELAY_PROVIDERS = frozenset({"jumphost", "ssh-bastion"})
RELAY_IMAGE = (
    "docker.io/alpine/socat:1.8.1.3"
    "@sha256:3ed1cd38741bd445ebffa3aecb9d80c46a83db45f5b5ef03332976c7ca9814af"
)
BUILD_WORKERS = frozenset({"activity-worker", "review-worker"})
LITELLM_FILES = ("litellm-config.yaml", "litellm_redaction.py")
REBAR_STORE_AWS_CONFIG = "rebar-store-aws-config"
TENANTS_HOST_ROOT = "/var/lib/agentic-dev-platform/tenants"
TENANTS_CLAIM = "tenants"
REVIEW_WORKER = "review-worker"
APP_UID = 10001
PLATFORM_ENV = "agentic-platform-env"
CONFIG_VOLUME = "platform-config"
CONFIG_SECRETS = "platform-config-secrets"
TENANT_BUNDLE = "tenant-config"
TENANT_SETTINGS_HASH = "agentic-dev-platform/tenant-settings-hash"
# The secrets each tenant holds on its own in AWS, plus the identity it signs its own ticket
# store with, which exists only in that store.
TENANT_OWNED_SETTINGS = frozenset(
    {
        "GITHUB_TOKEN",
        "JIRA_API_TOKEN",
        "JIRA_PAT",
        "REGISTRY_TOKEN",
        "SLACK_WEBHOOK_URL",
        "AUTHORSHIP_SIGNING_KEY",
        "AUTHORSHIP_SIGNING_KEY_PATH",
        "AUTHORSHIP_IDENTITY_ID",
        "MCP_GATEWAY_TOKEN",
    }
)
TENANT_OWNED_FILES = frozenset({"authorship-signing-key", "authorship-signing-key.pub"})
# The review worker signs verdicts, so no activity worker receives the private review key, as no
# activity worker identity can read it in AWS.
REVIEW_WORKER_FILES = frozenset({"review-signing-key"})
REPO_INTELLIGENCE_CALLER_ENV = "REPO_INTELLIGENCE_TOKEN"
WEBHOOK_SECRET_SETTINGS = ("GITHUB_WEBHOOK_SECRET", "JIRA_WEBHOOK_SECRET")


@dataclass(frozen=True)
class RenderInputs:
    settings: Settings
    env: Mapping[str, str | None]
    network: ComposeNetwork
    build_id: str
    image_registry: str = ""
    root: Path = Path(".")


def kustomize(overlay: Path) -> list[dict[str, Any]]:
    rendered = process.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(overlay)],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _metadata(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": namespace,
        "labels": {"app.kubernetes.io/part-of": PLATFORM_NAMESPACE},
    }


def config_map(name: str, data: Mapping[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _metadata(name, PLATFORM_NAMESPACE),
        "data": dict(data),
    }


def secret(name: str, data: Mapping[str, str]) -> dict[str, Any]:
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": _metadata(name, PLATFORM_NAMESPACE),
        "data": encoded,
    }


def _files(directory: Path, names: Iterable[str]) -> dict[str, str]:
    return {name: (directory / name).read_text(encoding="utf-8") for name in names}


def operator_config(directory: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Split config/<env> into the YAML files a ConfigMap carries and the rest a Secret carries.

    The templates are left out. Every other file, such as a signing key or the ticket store's
    AWS profiles, can hold a credential.
    """
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and not path.name.endswith(".example") and not path.name.startswith(".")
    )
    yaml_files = {
        path.name: path.read_text(encoding="utf-8") for path in files if path.suffix == ".yaml"
    }
    other = {
        path.name: path.read_text(encoding="utf-8") for path in files if path.suffix != ".yaml"
    }
    return yaml_files, other


def generated_objects(inputs: RenderInputs) -> list[dict[str, Any]]:
    settings, network, root = inputs.settings, inputs.network, inputs.root
    address = settings.k8s_compose_address
    present = {key: value for key, value in inputs.env.items() if value is not None}
    config_yaml, config_secret = operator_config(root / "config" / settings.environment)
    if REBAR_STORE_AWS_CONFIG in config_secret:
        config_secret[REBAR_STORE_AWS_CONFIG], _ = rewrite_address(
            config_secret[REBAR_STORE_AWS_CONFIG], network, address
        )
    policies = root / "config" / "policies"
    platform_env = {
        key: value
        for key, value in rewrite_env(inputs.env, network, address).values.items()
        if not _withheld_from_pods(key)
    }
    return [
        secret(PLATFORM_ENV, platform_env),
        secret("agentic-platform", workload_secret_values(present, network, address)),
        config_map("agentic-platform", endpoint_values(settings, network, address)),
        config_map("platform-config", config_yaml),
        secret("platform-config-secrets", config_secret),
        config_map(
            "promotion-policies",
            _files(policies, sorted(path.name for path in policies.glob("*.rego"))),
        ),
        config_map("litellm-config", _files(root / "config" / "platform", LITELLM_FILES)),
    ]


def _pod_spec(document: Mapping[str, Any]) -> dict[str, Any] | None:
    if document.get("kind") != "Deployment":
        return None
    return document["spec"]["template"]["spec"]


def set_images(documents: list[dict[str, Any]], tag: str, registry: str = "") -> None:
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    for document in documents:
        spec = _pod_spec(document)
        if spec is None:
            continue
        for container in [*spec.get("initContainers", []), *spec.get("containers", [])]:
            for image in (PLATFORM_IMAGE, OPA_IMAGE, LITELLM_IMAGE):
                if container.get("image") == f"{image}:{LOCAL_TAG}":
                    container["image"] = f"{prefix}{image}:{tag}"


def set_build_id(documents: list[dict[str, Any]], build_id: str) -> None:
    for document in documents:
        spec = _pod_spec(document)
        if spec is None or document["metadata"]["name"] not in BUILD_WORKERS:
            continue
        for container in spec["containers"]:
            if container["name"] == document["metadata"]["name"]:
                container.setdefault("env", []).append(
                    {"name": "AGENT_BUILD_ID", "value": build_id}
                )


def relay_container(settings: Settings) -> dict[str, Any]:
    target = f"{settings.k8s_compose_address}:{settings.bedrock_tunnel_local_port}"
    return {
        "name": "bedrock-relay",
        "restartPolicy": "Always",
        "image": RELAY_IMAGE,
        "args": ["TCP-LISTEN:443,fork,reuseaddr", f"TCP:{target}"],
        "resources": {
            "requests": {"cpu": "10m", "memory": "16Mi"},
            "limits": {"cpu": "250m", "memory": "64Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "runAsNonRoot": True,
            "runAsUser": 65534,
            "runAsGroup": 65534,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def add_bedrock_relay(documents: list[dict[str, Any]], settings: Settings) -> None:
    """Relay the workstation's Bedrock tunnel into each pod that calls Bedrock.

    The pod resolves the Bedrock runtime hostname to loopback, where the relay listens on 443
    and forwards to the tunnel, so TLS still verifies against the Bedrock certificate. The relay
    is a sidecar init container, so it outlives a client that drains during shutdown.
    """
    if settings.bedrock_connectivity_provider not in RELAY_PROVIDERS:
        return
    for document in documents:
        spec = _pod_spec(document)
        annotations = (
            document["spec"]["template"].get("metadata", {}).get("annotations", {})
            if spec is not None
            else {}
        )
        if spec is None or annotations.get(BEDROCK_CLIENT) != "true":
            continue
        spec.setdefault("initContainers", []).append(relay_container(settings))
        spec.setdefault("hostAliases", []).append(
            {"ip": "127.0.0.1", "hostnames": [settings.bedrock_runtime_host]}
        )
        security = spec.setdefault("securityContext", {})
        security.setdefault("sysctls", []).append(
            {"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"}
        )


def _retarget_url(value: str, namespaces: Mapping[str, str]) -> str:
    for old, new in namespaces.items():
        value = value.replace(f".{old}.svc", f".{new}.svc")
    return value


def _retarget(value: Any, namespaces: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return namespaces.get(value, _retarget_url(value, namespaces))
    if isinstance(value, list):
        return [_retarget(item, namespaces) for item in value]
    if isinstance(value, dict):
        return {key: _retarget(item, namespaces) for key, item in value.items()}
    return value


def retarget_namespaces(documents: list[dict[str, Any]], namespaces: Mapping[str, str]) -> list:
    """Move the platform and sandbox resources into the namespaces the settings name.

    Only the fields that name a namespace change, so a label value equal to a namespace name,
    such as app.kubernetes.io/part-of, is left alone.
    """
    changed = {old: new for old, new in namespaces.items() if old != new}
    if not changed:
        return documents
    result = []
    for document in documents:
        document = copy.deepcopy(document)
        metadata = document["metadata"]
        if document["kind"] == "Namespace":
            metadata["name"] = changed.get(metadata["name"], metadata["name"])
        if "namespace" in metadata:
            metadata["namespace"] = changed.get(metadata["namespace"], metadata["namespace"])
        for subject in document.get("subjects", []):
            if "namespace" in subject:
                subject["namespace"] = changed.get(subject["namespace"], subject["namespace"])
        if document["kind"] == "NetworkPolicy":
            document["spec"] = _retarget(document["spec"], changed)
        spec = _pod_spec(document)
        if spec is not None:
            for container in spec.get("containers", []):
                for variable in container.get("env", []):
                    if "value" in variable:
                        variable["value"] = _retarget_url(variable["value"], changed)
        result.append(document)
    return result


def namespaces(overlay: Path) -> list[dict[str, Any]]:
    text = (overlay / "namespaces.yaml").read_text(encoding="utf-8")
    return [document for document in yaml.safe_load_all(text) if document]


def stamp_config_hash(documents: list[dict[str, Any]], generated: list[dict[str, Any]]) -> None:
    """Annotate each pod template with a digest of the generated config and the images.

    A ConfigMap or Secret change leaves a Deployment's template unchanged, so the digest is what
    makes an apply roll the pods onto new config, and an unchanged apply rolls nothing.
    """
    config = json.dumps([document["data"] for document in generated], sort_keys=True)
    for document in documents:
        spec = _pod_spec(document)
        if spec is None:
            continue
        images = json.dumps(
            [c.get("image") for c in [*spec.get("initContainers", []), *spec["containers"]]]
        )
        digest = hashlib.sha256(f"{config}{images}".encode()).hexdigest()[:16]
        template = document["spec"]["template"].setdefault("metadata", {})
        template.setdefault("annotations", {})[CONFIG_HASH] = digest


def _host_volume(name: str, path: str, access: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": PLATFORM_NAMESPACE}},
        "spec": {
            "capacity": {"storage": "50Gi"},
            "accessModes": [access],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "hostPath": {"path": path, "type": "DirectoryOrCreate"},
        },
    }


def _claim(name: str, namespace: str, volume: str, access: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": _metadata(name, namespace),
        "spec": {
            "accessModes": [access],
            "storageClassName": "",
            "volumeName": volume,
            "resources": {"requests": {"storage": "50Gi"}},
        },
    }


def _volume_owner(image: str, mount_path: str) -> dict[str, Any]:
    """Return the init container that hands the new hostPath directory to the platform user.

    The kubelet creates a DirectoryOrCreate hostPath owned by root with mode 0755.
    """
    return {
        "name": "workspace-owner",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["chown", f"{APP_UID}:{APP_UID}", mount_path],
        "securityContext": {
            "runAsUser": 0,
            "runAsNonRoot": False,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]},
        },
        "volumeMounts": [{"name": "workspaces", "mountPath": mount_path}],
    }


def local_tenants(
    settings: Settings, documents: list[dict[str, Any]], root: Path = Path(".")
) -> list[Any]:
    """Return each tenant TENANTS names, or the TENANT of this workstation, with its workspace."""
    from services.orchestration.task_queues import tenant_names

    from scripts.tenant_render import WORKER, WORKSPACE_CLAIM, Tenant, tenant_root, worker_env

    names = tenant_names(settings) or ([settings.tenant] if settings.tenant else [])
    if not names:
        raise ValueError("set TENANTS or TENANT, because every activity worker serves a tenant")
    worker = next(
        d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == WORKER
    )
    image = worker["spec"]["template"]["spec"]["containers"][0]["image"]
    from services.integration.image_policy import load_policy

    policy = load_policy(settings.environment, root)
    tenants = []
    for name in names:
        volume = f"tenant-{name}-workspace"
        tenants.append(
            Tenant(
                name=name,
                env=worker_env(name),
                workspace=(
                    _host_volume(volume, f"{TENANTS_HOST_ROOT}/{name}", "ReadWriteOnce"),
                    _claim(WORKSPACE_CLAIM, f"tenant-{name}", volume, "ReadWriteOnce"),
                ),
                init_containers=(_volume_owner(image, tenant_root(name)),),
                image_prefixes=tuple(entry for entry, _ in policy.entries(name)),
            )
        )
    return tenants


def mount_tenants_on_review(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give the review worker every tenant's workspace read-only at /tenants, as on ECS."""
    from scripts.tenant_render import WORKSPACE_CLAIM

    review = next(
        (
            d
            for d in documents
            if d["kind"] == "Deployment" and d["metadata"]["name"] == REVIEW_WORKER
        ),
        None,
    )
    if review is None:
        return documents
    spec = review["spec"]["template"]["spec"]
    for volume in spec.get("volumes", []):
        if (volume.get("persistentVolumeClaim") or {}).get("claimName") == WORKSPACE_CLAIM:
            volume["name"] = TENANTS_CLAIM
            volume["persistentVolumeClaim"] = {"claimName": TENANTS_CLAIM, "readOnly": True}
    for container in spec["containers"]:
        for mount in container.get("volumeMounts", []):
            if mount["name"] == WORKSPACE_CLAIM:
                mount.update(name=TENANTS_CLAIM, mountPath="/tenants", readOnly=True)
    volume = "agentic-platform-tenants"
    return documents + [
        _host_volume(volume, TENANTS_HOST_ROOT, "ReadOnlyMany"),
        _claim(TENANTS_CLAIM, PLATFORM_NAMESPACE, volume, "ReadOnlyMany"),
    ]


def tenant_bundle(directory: Path) -> dict[str, str]:
    """Return the YAML files of a tenant's bundle directory, the files its config source reads."""
    if not directory.is_dir():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix == ".yaml"
    }


def home_tenant(settings: Settings, names: Sequence[str]) -> str:
    """Return the tenant whose settings `.env` holds, TENANT or else the first TENANTS names."""
    return settings.tenant if settings.tenant in names else names[0]


def _pod_settings(
    values: Mapping[str, str], network: ComposeNetwork, address: str
) -> dict[str, str]:
    return {
        key: rewrite_address(value, network, address)[0]
        for key, value in values.items()
        if key not in WORKSTATION_SETTINGS
    }


def _document(documents: Iterable[dict[str, Any]], kind: str, namespace: str, name: str) -> dict:
    return next(
        d
        for d in documents
        if d["kind"] == kind
        and d["metadata"].get("namespace") == namespace
        and d["metadata"]["name"] == name
    )


def _project_bundle(worker: dict[str, Any], tenant: str, files: Iterable[str]) -> None:
    """Add the bundle to the worker's operator config volume under tenants/<tenant>/.

    Explicit items keep the bundle files out of the volume root, where they would replace the
    environment's files of the same name.
    """
    items = [{"key": name, "path": f"tenants/{tenant}/{name}"} for name in files]
    for volume in worker["spec"]["template"]["spec"]["volumes"]:
        if volume["name"] == CONFIG_VOLUME:
            volume["projected"]["sources"].append(
                {"configMap": {"name": TENANT_BUNDLE, "items": items}}
            )


@dataclass(frozen=True)
class TicketStore:
    repo_root: str
    bucket: str
    prefix: str


def ticket_store(bundle: Mapping[str, str], environment: Path) -> TicketStore:
    """Return the rebar store that the tenant's bundle or the environment's tickets.yaml names."""
    text = bundle.get("tickets.yaml")
    if text is None:
        path = environment / "tickets.yaml"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    raw = yaml.safe_load(text) or {}
    backend, store = raw.get("backend") or {}, raw.get("store") or {}
    return TicketStore(
        repo_root=str(backend.get("repo_root") or "").strip().rstrip("/"),
        bucket=str(store.get("bucket") or "").strip(),
        prefix=str(store.get("prefix") or "").strip().strip("/"),
    )


def _prefixes_overlap(first: str, second: str) -> bool:
    return (
        not first
        or not second
        or first == second
        or first.startswith(f"{second}/")
        or second.startswith(f"{first}/")
    )


def refuse_shared_stores(stores: Mapping[str, TicketStore]) -> None:
    """Refuse a rebar ticket store outside its tenant's workspace or overlapping another's."""
    from scripts.tenant_render import tenant_root

    for tenant, store in stores.items():
        root = tenant_root(tenant)
        if not store.repo_root.startswith(f"{root}/"):
            raise ValueError(
                f"tenant {tenant} keeps its rebar ticket store at {store.repo_root or 'no path'}, "
                f"outside its workspace {root}. Set backend.repo_root under {root} in its "
                "tickets.yaml"
            )
    for (first, one), (second, other) in itertools.combinations(stores.items(), 2):
        shared_bucket = one.bucket and one.bucket == other.bucket
        if shared_bucket and _prefixes_overlap(one.prefix, other.prefix):
            raise ValueError(
                f"tenants {first} and {second} sync their rebar ticket stores to overlapping "
                f"locations in bucket {one.bucket}, prefixes {one.prefix or '(root)'} and "
                f"{other.prefix or '(root)'}. Give each tenant its own store.prefix"
            )


def _refuse_shared_schedules(pod_env: Mapping[str, Mapping[str, str]]) -> None:
    seen: dict[str, str] = {}
    for tenant, values in pod_env.items():
        schedule = values.get("INTAKE_SCHEDULE_ID", "")
        if not schedule:
            continue
        if schedule in seen:
            raise ValueError(
                f"tenants {seen[schedule]} and {tenant} share INTAKE_SCHEDULE_ID {schedule}, so "
                "each worker would replace the other's intake schedule"
            )
        seen[schedule] = tenant


def _repo_intelligence_credential(name: str) -> bool:
    """Return whether the setting is a repository intelligence token or the token key."""
    from services.repository_intelligence.tenant_tokens import ENV_PREFIX

    return name == REPO_INTELLIGENCE_CALLER_ENV or name.startswith(ENV_PREFIX)


def _webhook_secret(name: str) -> bool:
    return any(
        name == setting or name.startswith(f"{setting}_") for setting in WEBHOOK_SECRET_SETTINGS
    )


def _withheld_from_pods(name: str) -> bool:
    """Return whether no pod's settings Secret may carry the setting.

    The inbound webhook reads the webhook signing secrets from Secrets of its own.
    """
    return _repo_intelligence_credential(name) or _webhook_secret(name)


def _refuse_tenant_tokens(own: Mapping[str, Mapping[str, str] | None], environment: str) -> None:
    for name, values in own.items():
        refused = sorted(key for key in values or {} if _repo_intelligence_credential(key))
        if refused:
            raise ValueError(
                f"config/{environment}/tenants/{name}.env sets {', '.join(refused)}. The render "
                "gives each tenant's worker its own repository intelligence token, so remove the "
                "setting from that file"
            )


def _find_document(
    documents: list[dict[str, Any]], kind: str, namespace: str, name: str
) -> dict[str, Any] | None:
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


def _refuse_gateway_tokens(
    pod_env: Mapping[str, Mapping[str, str]],
    home: str,
    own: Mapping[str, Mapping[str, str] | None],
    environment: str,
) -> None:
    """Refuse a tenant without its own MCP gateway token, or two tenants sharing one.

    Each tenant's gateway accepts only its tenant's token, so a shared token would open every
    sharing tenant's gateway to the holder.
    """
    holders: dict[str, list[str]] = {}
    for name, values in pod_env.items():
        token = (values.get("MCP_GATEWAY_TOKEN") or "").strip()
        if not token:
            source = (
                ".env"
                if name == home and own.get(name) is None
                else f"config/{environment}/tenants/{name}.env"
            )
            raise ValueError(
                f"tenant {name} has no MCP_GATEWAY_TOKEN. Set a token of its own in {source}, "
                "for example the output of `openssl rand -hex 32`"
            )
        holders.setdefault(token, []).append(name)
    shared = [sorted(names) for names in holders.values() if len(names) > 1]
    if shared:
        raise ValueError(
            f"tenants {', '.join(shared[0])} share one MCP_GATEWAY_TOKEN. Give each tenant a "
            f"token of its own in config/{environment}/tenants/<tenant>.env"
        )


def localize_tenants(
    documents: list[dict[str, Any]], inputs: RenderInputs, names: Iterable[str]
) -> list[dict[str, Any]]:
    """Give each tenant's worker its own config bundle and its own settings.

    The home tenant without a settings file keeps every setting of `.env`, the single-tenant
    behaviour. Every other tenant receives the shared settings plus its own file's values.
    With more than one tenant, each tenant other than the home tenant must name its own
    repositories, and each rebar tenant's store must lie in its own workspace and sync to its
    own bucket location.

    Each tenant's MCP gateway reads the same settings and bundle as its worker, so each tenant
    must hold an MCP_GATEWAY_TOKEN of its own, which no other tenant shares.

    Every tenant's worker, the home tenant's included, receives its own repository intelligence
    token and none of the repository intelligence tokens or the key of `.env`. A tenant's
    settings file may not set them. The worker's settings hash covers the token, so a new token
    restarts the worker.

    The bundle is left out of the config hash, so an apply that changes only bundle contents
    reaches the running worker through the kubelet's ConfigMap sync without a restart.
    """
    from services.integration.tickets.backend import REBAR
    from services.repository_intelligence.tenant_tokens import tenant_tokens

    from scripts.tenant_config import apply_tenant_settings, read_tenant_settings
    from scripts.tenant_render import GATEWAY, WORKER, worker_namespace

    settings, network = inputs.settings, inputs.network
    address = settings.k8s_compose_address
    environment = inputs.root / "config" / settings.environment
    directory = environment / "tenants"
    names = list(names)
    home = home_tenant(settings, names)
    own = {name: read_tenant_settings(directory, name) for name in names}
    _refuse_tenant_tokens(own, settings.environment)
    for values in own.values():
        if values:
            apply_tenant_settings(settings, values)
    owned = TENANT_OWNED_SETTINGS.union(*(values for values in own.values() if values))
    workstation = {
        key: value
        for key, value in rewrite_env(inputs.env, network, address).values.items()
        if not _withheld_from_pods(key)
    }
    shared = {key: value for key, value in workstation.items() if key not in owned}
    present = {key: value for key, value in inputs.env.items() if value is not None}
    tokens = tenant_tokens(
        names, key=present.get("REPO_INTELLIGENCE_TOKEN_KEY", "").strip(), environ=present
    )
    default_backend = str(Settings.model_fields["ticket_backend"].default)
    result = list(documents)
    pod_env: dict[str, dict[str, str]] = {}
    stores: dict[str, TicketStore] = {}
    for name in names:
        namespace = worker_namespace(name)
        bundle = tenant_bundle(directory / name)
        if len(names) > 1 and name != home and "repositories.yaml" not in bundle:
            raise ValueError(
                f"tenant {name} names no repositories of its own. Copy "
                f"config/{settings.environment}/tenants/beta.example/ to "
                f"config/{settings.environment}/tenants/{name}/ and edit its repositories.yaml"
            )
        worker = _document(result, "Deployment", namespace, WORKER)
        gateway = _find_document(result, "Deployment", namespace, GATEWAY)
        pods = [worker] if gateway is None else [worker, gateway]
        if bundle:
            bundle_map = config_map(TENANT_BUNDLE, bundle)
            bundle_map["metadata"]["namespace"] = namespace
            result.append(bundle_map)
            for pod in pods:
                _project_bundle(pod, name, sorted(bundle))
        values = own[name]
        files = _document(result, "Secret", namespace, CONFIG_SECRETS)
        if values is None and name == home:
            pod_env[name] = dict(workstation)
            withheld = REVIEW_WORKER_FILES
            hashed: dict[str, str] | None = None
        else:
            withheld = REVIEW_WORKER_FILES | TENANT_OWNED_FILES
            hashed = {
                key: value for key, value in (values or {}).items() if not _webhook_secret(key)
            }
            pod_env[name] = {**shared, **_pod_settings(hashed, network, address)}
        pod_env[name] = {
            key: qualify_in_cluster_services(value, settings.k8s_namespace)
            for key, value in pod_env[name].items()
        }
        if name in tokens:
            pod_env[name][REPO_INTELLIGENCE_CALLER_ENV] = tokens[name]
            hashed = {**(hashed or {}), REPO_INTELLIGENCE_CALLER_ENV: tokens[name]}
        env = _document(result, "Secret", namespace, PLATFORM_ENV)
        env["data"] = secret(PLATFORM_ENV, pod_env[name])["data"]
        if hashed is not None:
            digest = hashlib.sha256(json.dumps(hashed, sort_keys=True).encode()).hexdigest()[:16]
            for pod in pods:
                template = pod["spec"]["template"].setdefault("metadata", {})
                template.setdefault("annotations", {})[TENANT_SETTINGS_HASH] = digest
        files["data"] = {key: value for key, value in files["data"].items() if key not in withheld}
        backend = pod_env[name].get("TICKET_BACKEND") or default_backend
        if backend.strip().lower() == REBAR:
            stores[name] = ticket_store(bundle, environment)
    if len(names) > 1:
        refuse_shared_stores(stores)
        _refuse_shared_schedules(pod_env)
    if any(_find_document(result, "Deployment", worker_namespace(n), GATEWAY) for n in names):
        _refuse_gateway_tokens(pod_env, home, own, settings.environment)
    return result


def render(inputs: RenderInputs, overlay: Path = OVERLAY) -> list[dict[str, Any]]:
    """Return the namespaces, then the overlay, then the generated config, ready to apply."""
    from scripts.tenant_render import tenantize
    from scripts.webhook_render import localize_webhook

    directory = inputs.root / overlay
    generated = generated_objects(inputs)
    documents = namespaces(directory) + kustomize(directory) + generated
    set_images(documents, inputs.build_id, inputs.image_registry)
    set_build_id(documents, inputs.build_id)
    add_bedrock_relay(documents, inputs.settings)
    tenants = local_tenants(inputs.settings, documents, inputs.root)
    documents = localize_tenants(
        tenantize(mount_tenants_on_review(documents), tenants),
        inputs,
        [tenant.name for tenant in tenants],
    )
    documents = localize_webhook(documents, inputs, [tenant.name for tenant in tenants])
    stamp_config_hash(documents, generated)
    return retarget_namespaces(
        documents,
        {
            PLATFORM_NAMESPACE: inputs.settings.k8s_namespace,
            SANDBOX_NAMESPACE: inputs.settings.openhands_sandbox_namespace,
        },
    )


def dump(documents: Iterable[Mapping[str, Any]]) -> str:
    return yaml.safe_dump_all(list(documents), sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from dotenv import dotenv_values

    from scripts.cluster_env import compose_config, compose_network
    from scripts.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--compose-file", action="append", default=[], dest="compose_files")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    settings = get_settings()
    inputs = RenderInputs(
        settings=settings,
        env=dotenv_values(args.env_file),
        network=compose_network(compose_config(args.compose_files)),
        build_id=args.build_id,
    )
    sys.stdout.write(dump(render(inputs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
