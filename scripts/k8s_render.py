"""Render the local cluster's manifests with the generated config, secrets, and build image.

The kustomize overlay holds the static shape. This adds what is derived from the workstation
at deploy time: the platform settings from config/local/platform.env with the compose addresses
rewritten for the pods, the operator config files, the promotion policies, the LiteLLM config,
the image of the current build, the Bedrock tunnel relay when the connectivity provider needs
one, each tenant's worker and sandbox namespaces, and the namespaces the settings name.

Each tenant's workspace is a hostPath directory on the node under TENANTS_HOST_ROOT, and the
tenant's review worker in tenant-<tenant>-review mounts only that tenant's directory, read-only at
/tenants/<tenant>, the layout the EFS access points give the workers in AWS.

Each tenant's review worker receives the tenant's bundle, the tenant's settings without its
integration credentials, the tenant's REGISTRY_TOKEN, and REVIEW_SIGNING_KEY of the tenant's
settings file, which no other pod receives. No pod receives a review signing key path, and the
render refuses a platform config/local/review-signing-key file.

The tenants are the folders of config/local/tenants/ with their settings files, as
scripts.local_tenants reads them, and every pod receives the list as TENANTS. The files of
<tenant>/ are that tenant's config bundle, which only its activity worker and MCP gateway mount,
at the path their directory config source reads. <tenant>.env holds the settings and credentials
only that tenant's worker and gateway receive, applied over the platform settings. The platform
settings file holds none of the TENANT_OWNED_SETTINGS, so one tenant's credentials never reach
another tenant's pods.

Each tenant's worker receives its own repository intelligence token as REPO_INTELLIGENCE_TOKEN,
the token repository intelligence expects of that tenant, which it derives from
REPO_INTELLIGENCE_TOKEN_KEY unless the platform settings set REPO_INTELLIGENCE_TOKEN_<TENANT>. No
cluster pod receives the key, and no tenant's worker receives another tenant's token.

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
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from services import process
from services.integration.credentials import env_config_dir
from services.integration.tenant_config import refuse_platform_tenant_files
from services.orchestration.platform_escalation import PLATFORM_ESCALATION_FILE

from scripts.cluster_env import (
    WORKSTATION_SETTINGS,
    ComposeNetwork,
    endpoint_values,
    qualify_in_cluster_services,
    rewrite_address,
    rewrite_env,
    workload_secret_values,
)
from scripts.config import TENANT_OWNED_SETTINGS, Settings, refuse_tenant_settings
from scripts.eks_render import (
    CONFIG_HASH,
    LITELLM_FILES,
    PLATFORM_NAMESPACE,
    _pod_spec,
    config_map,
)
from scripts.env_file import PLATFORM_ENV_FILE

OVERLAY = Path("deploy/kubernetes/local")
SANDBOX_NAMESPACE = "agent-sandbox"
PLATFORM_IMAGE = "agentic-dev-platform-local"
OPA_IMAGE = "agentic-dev-platform-opa"
LITELLM_IMAGE = "agentic-dev-platform-litellm"
LOCAL_TAG = "local"
BEDROCK_CLIENT = "agentic-dev-platform/bedrock-client"
RELAY_PROVIDERS = frozenset({"jumphost", "ssh-bastion"})
RELAY_IMAGE = (
    "docker.io/alpine/socat:1.8.1.3"
    "@sha256:3ed1cd38741bd445ebffa3aecb9d80c46a83db45f5b5ef03332976c7ca9814af"
)
BUILD_WORKERS = frozenset({"activity-worker", "review-worker"})
REBAR_STORE_AWS_CONFIG = "rebar-store-aws-config"
TENANTS_HOST_ROOT = "/var/lib/agentic-dev-platform/tenants"
RETIRED_PLATFORM_REVIEW_OBJECTS = (
    ("persistentvolumeclaim", PLATFORM_NAMESPACE, "tenants"),
    ("persistentvolume", "", "agentic-platform-tenants"),
    ("configmap", PLATFORM_NAMESPACE, "platform-config"),
    ("secret", PLATFORM_NAMESPACE, "platform-config-secrets"),
)
APP_UID = 10001
PLATFORM_ENV = "agentic-platform-env"
CONFIG_VOLUME = "platform-config"
CONFIG_SECRETS = "platform-config-secrets"
TENANT_BUNDLE = "tenant-config"
TENANT_SETTINGS_HASH = "agentic-dev-platform/tenant-settings-hash"
TENANT_OWNED_FILES = frozenset({"authorship-signing-key", "authorship-signing-key.pub"})
PLATFORM_REVIEW_SIGNING_KEY_FILE = "review-signing-key"
REVIEW_SIGNING_KEY_ENV = "REVIEW_SIGNING_KEY"
REVIEW_SIGNING_KEY_SETTINGS = frozenset({REVIEW_SIGNING_KEY_ENV, "REVIEW_SIGNING_KEY_PATH"})
REPO_INTELLIGENCE_CALLER_ENV = "REPO_INTELLIGENCE_TOKEN"
REVIEW_WITHHELD_SETTINGS = (TENANT_OWNED_SETTINGS - {"REGISTRY_TOKEN"}) | {
    REPO_INTELLIGENCE_CALLER_ENV
}
WEBHOOK_SECRET_SETTINGS = ("GITHUB_WEBHOOK_SECRET", "JIRA_WEBHOOK_SECRET", "SLACK_SIGNING_SECRET")
SHARED_SLACK_SECRETS = frozenset(
    {"SLACK_SHARED_BOT_TOKEN", "SLACK_SHARED_SIGNING_SECRET", "SLACK_SHARED_APP_TOKEN"}
)
UNSHARED_SLACK_SETTINGS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ESCALATION_ACTION_KEY")


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


def secret(name: str, data: Mapping[str, str]) -> dict[str, Any]:
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": _metadata(name, PLATFORM_NAMESPACE),
        "data": encoded,
    }


def settings_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def _files(directory: Path, names: Iterable[str]) -> dict[str, str]:
    return {name: (directory / name).read_text(encoding="utf-8") for name in names}


def operator_config(directory: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Split config/local into the YAML files a ConfigMap carries and the rest a Secret carries.

    The templates and the settings files are left out, because the render passes each setting
    to the pods that read it. Every other file, such as a signing key or the ticket store's AWS
    profiles, can hold a credential.
    """
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.name.endswith(".example")
        and not path.name.startswith(".")
        and path.suffix != ".env"
    )
    yaml_files = {
        path.name: path.read_text(encoding="utf-8") for path in files if path.suffix == ".yaml"
    }
    other = {
        path.name: path.read_text(encoding="utf-8") for path in files if path.suffix != ".yaml"
    }
    return yaml_files, other


def platform_escalation_file(root: Path) -> dict[str, str]:
    """Return the platform-escalation.yaml of config/local, or else of config/platform."""
    for directory in (env_config_dir(root=root), root / "config" / "platform"):
        path = directory / PLATFORM_ESCALATION_FILE
        if path.is_file():
            return {PLATFORM_ESCALATION_FILE: path.read_text(encoding="utf-8")}
    return {}


def _refuse_platform_review_signing_key(directory: Path) -> None:
    if (directory / PLATFORM_REVIEW_SIGNING_KEY_FILE).exists():
        raise ValueError(
            f"config/local/{PLATFORM_REVIEW_SIGNING_KEY_FILE} holds a platform review signing "
            "key, and each tenant's review worker signs with a key of its own. Set "
            "REVIEW_SIGNING_KEY in each tenant's settings file, "
            f"config/local/tenants/<tenant>.env, and remove "
            f"config/local/{PLATFORM_REVIEW_SIGNING_KEY_FILE}"
        )


def generated_objects(inputs: RenderInputs, names: Sequence[str]) -> list[dict[str, Any]]:
    """Return the platform's generated config, whose settings name every tenant in TENANTS."""
    settings, network, root = inputs.settings, inputs.network, inputs.root
    address = settings.k8s_compose_address
    present = {key: value for key, value in inputs.env.items() if value is not None}
    refuse_tenant_settings(present, PLATFORM_ENV_FILE)
    refuse_platform_tenant_files(env_config_dir(root=root))
    _refuse_platform_review_signing_key(env_config_dir(root=root))
    config_yaml, config_secret = operator_config(env_config_dir(root=root))
    config_yaml.update(platform_escalation_file(root))
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
    platform_env["TENANTS"] = ",".join(names)
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
    names: Sequence[str], documents: list[dict[str, Any]], root: Path = Path(".")
) -> list[Any]:
    """Return each named tenant with its workspace and its review worker's read-only workspace."""
    from scripts.tenant_render import (
        WORKER,
        WORKSPACE_CLAIM,
        Tenant,
        review_namespace,
        tenant_root,
        worker_env,
    )

    if not names:
        raise ValueError(
            "config/local/tenants/ holds no tenant, and every activity worker serves one. Copy "
            "config/local/tenants/tenant.example/ to config/local/tenants/<tenant>/ and "
            "config/local/tenants/tenant.example.env to config/local/tenants/<tenant>.env"
        )
    worker = next(
        d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"] == WORKER
    )
    image = worker["spec"]["template"]["spec"]["containers"][0]["image"]
    from services.integration.image_policy import load_policy

    policy = load_policy(root=root)
    tenants = []
    for name in names:
        volume = f"tenant-{name}-workspace"
        review_volume = f"tenant-{name}-review-workspace"
        host_path = f"{TENANTS_HOST_ROOT}/{name}"
        tenants.append(
            Tenant(
                name=name,
                env=worker_env(name),
                workspace=(
                    _host_volume(volume, host_path, "ReadWriteOnce"),
                    _claim(WORKSPACE_CLAIM, f"tenant-{name}", volume, "ReadWriteOnce"),
                ),
                init_containers=(_volume_owner(image, tenant_root(name)),),
                image_prefixes=tuple(entry for entry, _ in policy.entries(name)),
                review_workspace=(
                    _host_volume(review_volume, host_path, "ReadOnlyMany"),
                    _claim(WORKSPACE_CLAIM, review_namespace(name), review_volume, "ReadOnlyMany"),
                ),
            )
        )
    return tenants


def tenant_bundle(directory: Path) -> dict[str, str]:
    """Return the YAML files of a tenant's bundle directory, the files its config source reads."""
    if not directory.is_dir():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix == ".yaml"
    }


def _pod_settings(
    values: Mapping[str, str], network: ComposeNetwork, address: str
) -> dict[str, str]:
    return {
        key: rewrite_address(value, network, address)[0]
        for key, value in values.items()
        if key not in WORKSTATION_SETTINGS
    }


def _matching(
    documents: Iterable[dict[str, Any]], kind: str, namespace: str, name: str
) -> Iterator[dict[str, Any]]:
    return (
        d
        for d in documents
        if d["kind"] == kind
        and d["metadata"].get("namespace") == namespace
        and d["metadata"]["name"] == name
    )


def _document(documents: Iterable[dict[str, Any]], kind: str, namespace: str, name: str) -> dict:
    return next(_matching(documents, kind, namespace, name))


def _project_bundle(worker: dict[str, Any], tenant: str, files: Iterable[str]) -> None:
    """Add the bundle to the worker's operator config volume under tenants/<tenant>/.

    Explicit items keep the bundle files out of the volume root, where they would replace the
    platform's files of the same name.
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


def ticket_store(bundle: Mapping[str, str]) -> TicketStore:
    """Return the rebar store that the tenant's tickets.yaml names."""
    raw = yaml.safe_load(bundle.get("tickets.yaml") or "") or {}
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

    The inbound webhook reads the webhook signing secrets, SLACK_SHARED_SIGNING_SECRET,
    SLACK_SHARED_APP_TOKEN, and each tenant's action key from Secrets of its own, and the Slack
    posting service reads SLACK_SHARED_BOT_TOKEN from one. Only a tenant's review worker receives
    a review signing key, its own, which the render adds to its Secret alone.
    """
    from services.integration.tenant_secrets import ESCALATION_ACTION_KEY_ENV_PREFIX

    return (
        _repo_intelligence_credential(name)
        or _webhook_secret(name)
        or name.startswith(ESCALATION_ACTION_KEY_ENV_PREFIX)
        or name in SHARED_SLACK_SECRETS
        or name in REVIEW_SIGNING_KEY_SETTINGS
    )


def _refuse_shared_slack_credentials(
    pod_env: Mapping[str, Mapping[str, str]],
) -> None:
    """Refuse two tenants holding one Slack credential, as either could act as the other."""
    for setting in UNSHARED_SLACK_SETTINGS:
        holders: dict[str, list[str]] = {}
        for name, values in pod_env.items():
            value = (values.get(setting) or "").strip()
            if value:
                holders.setdefault(value, []).append(name)
        shared = [sorted(names) for names in holders.values() if len(names) > 1]
        if shared:
            raise ValueError(
                f"tenants {', '.join(shared[0])} share one {setting}. Give each tenant its own "
                "in config/local/tenants/<tenant>.env"
            )


def _refuse_claim_clashes(platform_dir: Path, backends: Mapping[str, str]) -> None:
    """Refuse two tenants naming one value only one tenant may hold, such as a Slack channel."""
    from services.integration.tenant_claims import bundle_claims, find_clashes

    from scripts.tenant_config import tenant_files

    claims = {
        name: bundle_claims(tenant_files(platform_dir / "tenants" / name), backend)
        for name, backend in backends.items()
    }
    clashes = find_clashes(claims)
    if clashes:
        raise ValueError(
            f"{clashes[0]}. Give each tenant a value of its own in config/local/tenants/<tenant>/"
        )


def _refuse_missing_registry_token(
    platform_dir: Path, tenant: str, bundle: Mapping[str, str], values: Mapping[str, str]
) -> None:
    """Refuse a tenant on an external registry without its registry identity or token."""
    from services.integration.credentials import ConfigError
    from services.orchestration.gates.registry_mirrors import (
        EXTERNAL,
        REGISTRY_CREDENTIAL_ENV,
        RegistryConfigError,
        parse_texts,
    )

    platform = platform_dir / "registries.yaml"
    if not platform.is_file():
        return
    try:
        config = parse_texts(platform.read_text(encoding="utf-8"), bundle.get("registries.yaml"))
    except (ConfigError, RegistryConfigError) as exc:
        raise ValueError(f"tenant {tenant}: {exc}") from exc
    if config.mode == EXTERNAL and not (values.get(REGISTRY_CREDENTIAL_ENV) or "").strip():
        raise ValueError(
            f"tenant {tenant} has no {REGISTRY_CREDENTIAL_ENV}, the token of its registry "
            f"identity. Set it in config/local/tenants/{tenant}.env"
        )


def _refuse_tenant_tokens(own: Mapping[str, Mapping[str, str] | None]) -> None:
    for name, values in own.items():
        shared = sorted(key for key in values or {} if key in SHARED_SLACK_SECRETS)
        if shared:
            raise ValueError(
                f"config/local/tenants/{name}.env sets {', '.join(shared)}. The shared "
                f"Slack app's credentials belong to the platform in {PLATFORM_ENV_FILE}, so "
                "remove the setting from that file"
            )
        refused = sorted(key for key in values or {} if _repo_intelligence_credential(key))
        if refused:
            raise ValueError(
                f"config/local/tenants/{name}.env sets {', '.join(refused)}. The render "
                "gives each tenant's worker its own repository intelligence token, so remove the "
                "setting from that file"
            )


def _find_document(
    documents: list[dict[str, Any]], kind: str, namespace: str, name: str
) -> dict[str, Any] | None:
    return next(_matching(documents, kind, namespace, name), None)


def _refuse_gateway_tokens(pod_env: Mapping[str, Mapping[str, str]]) -> None:
    """Refuse a tenant without its own MCP gateway token, or two tenants sharing one.

    Each tenant's gateway accepts only its tenant's token, so a shared token would open every
    sharing tenant's gateway to the holder.
    """
    holders: dict[str, list[str]] = {}
    for name, values in pod_env.items():
        token = (values.get("MCP_GATEWAY_TOKEN") or "").strip()
        if not token:
            raise ValueError(
                f"tenant {name} has no MCP_GATEWAY_TOKEN. Set a token of its own in "
                f"config/local/tenants/{name}.env, for example the output of "
                "`openssl rand -hex 32`"
            )
        holders.setdefault(token, []).append(name)
    shared = [sorted(names) for names in holders.values() if len(names) > 1]
    if shared:
        raise ValueError(
            f"tenants {', '.join(shared[0])} share one MCP_GATEWAY_TOKEN. Give each tenant a "
            "token of its own in config/local/tenants/<tenant>.env"
        )


def _localize_review_worker(
    documents: list[dict[str, Any]],
    review: dict[str, Any],
    tenant: str,
    bundle: Mapping[str, str],
    tenant_env: Mapping[str, str],
    signing_key: str | None,
) -> None:
    """Give the tenant's review worker the tenant's bundle, settings, and review signing key."""
    namespace = review["metadata"]["namespace"]
    bundle_map = config_map(TENANT_BUNDLE, bundle)
    bundle_map["metadata"]["namespace"] = namespace
    documents.append(bundle_map)
    _project_bundle(review, tenant, sorted(bundle))
    values = {
        key: value for key, value in tenant_env.items() if key not in REVIEW_WITHHELD_SETTINGS
    }
    if signing_key:
        values[REVIEW_SIGNING_KEY_ENV] = signing_key
    _document(documents, "Secret", namespace, PLATFORM_ENV)["data"] = secret(PLATFORM_ENV, values)[
        "data"
    ]
    files = _document(documents, "Secret", namespace, CONFIG_SECRETS)
    files["data"] = {
        key: value for key, value in files["data"].items() if key not in TENANT_OWNED_FILES
    }
    digest = settings_digest(values)
    template = review["spec"]["template"].setdefault("metadata", {})
    template.setdefault("annotations", {})[TENANT_SETTINGS_HASH] = digest


def _refuse_review_signing_keys(keys: Mapping[str, str], signing: Mapping[str, bool]) -> None:
    """Refuse a signing tenant without a review signing key, or two tenants sharing one."""
    for name, key in keys.items():
        if signing[name] and not key:
            raise ValueError(
                f"tenant {name} has no {REVIEW_SIGNING_KEY_ENV}, the private key its review "
                f"worker signs verdicts with. Set it in config/local/tenants/{name}.env to a key "
                f"that review-trust-root.yaml lists as an active key of tenant {name}, or set "
                "REVIEW_SIGNING_ENABLED=false"
            )
    holders: dict[str, list[str]] = {}
    for name, key in keys.items():
        if key:
            holders.setdefault(key, []).append(name)
    shared = [sorted(names) for names in holders.values() if len(names) > 1]
    if shared:
        raise ValueError(
            f"tenants {', '.join(shared[0])} share one {REVIEW_SIGNING_KEY_ENV}. Give each tenant "
            "a review signing key of its own in config/local/tenants/<tenant>.env"
        )


def localize_tenants(
    documents: list[dict[str, Any]], inputs: RenderInputs, names: Iterable[str]
) -> list[dict[str, Any]]:
    """Give each tenant's worker its own config bundle and its own settings.

    Each tenant receives the platform settings with its own settings file applied over them, and
    TENANTS, the list of every tenant. Each tenant must name its own repositories, and with more
    than one tenant, each rebar tenant's store must lie in its own workspace and sync to its own
    bucket location. No two tenants may claim one value only one tenant may hold, such as the
    channel of a tenant on the shared Slack app, a Slack app ID, or a Jira project and intake label.

    Each tenant's MCP gateway reads the same settings and bundle as its worker, so each tenant
    must hold an MCP_GATEWAY_TOKEN of its own, which no other tenant shares.

    Every tenant's worker receives its own repository intelligence token and none of the
    repository intelligence tokens or the key of the platform settings. A tenant's settings file
    may not set them. The worker's settings hash covers its settings file and the token, so a
    change to either restarts the worker.

    Each tenant's review worker reads the same bundle and settings without the tenant's
    integration credentials and repository intelligence token, and with the tenant's
    REVIEW_SIGNING_KEY, which no other pod receives. While review signing is enabled for the
    tenant, the tenant must hold a review signing key of its own, which no other tenant shares.

    The bundle is left out of the config hash, so an apply that changes only bundle contents
    reaches the running worker through the kubelet's ConfigMap sync without a restart.
    """
    from services.integration.tickets.backend import REBAR
    from services.repository_intelligence.tenant_tokens import tenant_tokens

    from scripts.tenant_config import apply_tenant_settings, read_tenant_settings
    from scripts.tenant_render import (
        GATEWAY,
        REVIEW_WORKER,
        WORKER,
        review_namespace,
        worker_namespace,
    )

    settings, network = inputs.settings, inputs.network
    address = settings.k8s_compose_address
    platform_dir = env_config_dir(root=inputs.root)
    directory = platform_dir / "tenants"
    names = list(names)
    own: dict[str, dict[str, str]] = {}
    for name in names:
        values = read_tenant_settings(directory, name)
        if values is None:
            raise ValueError(
                f"tenant {name} has no settings file. Copy "
                f"config/local/tenants/tenant.example.env to config/local/tenants/{name}.env"
            )
        own[name] = values
    _refuse_tenant_tokens(own)
    signing = {
        name: apply_tenant_settings(settings, values).review_signing_enabled
        for name, values in own.items()
    }
    platform = {
        key: value
        for key, value in rewrite_env(inputs.env, network, address).values.items()
        if not _withheld_from_pods(key)
    }
    present = {key: value for key, value in inputs.env.items() if value is not None}
    tokens = tenant_tokens(
        names, key=present.get("REPO_INTELLIGENCE_TOKEN_KEY", "").strip(), environ=present
    )
    listed = ",".join(names)
    default_backend = str(Settings.model_fields["ticket_backend"].default)
    result = list(documents)
    pod_env: dict[str, dict[str, str]] = {}
    stores: dict[str, TicketStore] = {}
    backends: dict[str, str] = {}
    review_keys: dict[str, str] = {}
    for name in names:
        namespace = worker_namespace(name)
        bundle = tenant_bundle(directory / name)
        if "repositories.yaml" not in bundle:
            raise ValueError(
                f"tenant {name} names no repositories. Copy "
                "config/local/tenants/tenant.example/ to "
                f"config/local/tenants/{name}/ and edit its repositories.yaml"
            )
        worker = _document(result, "Deployment", namespace, WORKER)
        gateway = _find_document(result, "Deployment", namespace, GATEWAY)
        pods = [worker] if gateway is None else [worker, gateway]
        bundle_map = config_map(TENANT_BUNDLE, bundle)
        bundle_map["metadata"]["namespace"] = namespace
        result.append(bundle_map)
        for pod in pods:
            _project_bundle(pod, name, sorted(bundle))
        files = _document(result, "Secret", namespace, CONFIG_SECRETS)
        hashed = {
            key: value
            for key, value in own[name].items()
            if not _webhook_secret(key) and key not in REVIEW_SIGNING_KEY_SETTINGS
        }
        pod_env[name] = {
            key: qualify_in_cluster_services(value, settings.k8s_namespace)
            for key, value in {
                **platform,
                **_pod_settings(hashed, network, address),
                "TENANTS": listed,
            }.items()
        }
        if name in tokens:
            pod_env[name][REPO_INTELLIGENCE_CALLER_ENV] = tokens[name]
            hashed[REPO_INTELLIGENCE_CALLER_ENV] = tokens[name]
        env = _document(result, "Secret", namespace, PLATFORM_ENV)
        env["data"] = secret(PLATFORM_ENV, pod_env[name])["data"]
        digest = settings_digest(hashed)
        for pod in pods:
            template = pod["spec"]["template"].setdefault("metadata", {})
            template.setdefault("annotations", {})[TENANT_SETTINGS_HASH] = digest
        files["data"] = {
            key: value for key, value in files["data"].items() if key not in TENANT_OWNED_FILES
        }
        review = _find_document(result, "Deployment", review_namespace(name), REVIEW_WORKER)
        if review is not None:
            review_keys[name] = (own[name].get(REVIEW_SIGNING_KEY_ENV) or "").strip()
            _localize_review_worker(
                result, review, name, bundle, pod_env[name], own[name].get(REVIEW_SIGNING_KEY_ENV)
            )
        backend = pod_env[name].get("TICKET_BACKEND") or default_backend
        backends[name] = backend
        if backend.strip().lower() == REBAR:
            stores[name] = ticket_store(bundle)
        _refuse_missing_registry_token(platform_dir, name, bundle, pod_env[name])
    if len(names) > 1:
        refuse_shared_stores(stores)
        _refuse_shared_schedules(pod_env)
        _refuse_shared_slack_credentials(pod_env)
        _refuse_claim_clashes(platform_dir, backends)
    if any(_find_document(result, "Deployment", worker_namespace(n), GATEWAY) for n in names):
        _refuse_gateway_tokens(pod_env)
    _refuse_review_signing_keys(review_keys, signing)
    return result


def render(inputs: RenderInputs, overlay: Path = OVERLAY) -> list[dict[str, Any]]:
    """Return the namespaces, then the overlay, then the generated config, ready to apply."""
    from scripts.local_tenants import local_tenant_names
    from scripts.tenant_render import tenantize
    from scripts.validation_job import set_validation_image
    from scripts.webhook_render import localize_webhook, slack_poster_tls_objects

    directory = inputs.root / overlay
    names = local_tenant_names(env_config_dir(root=inputs.root))
    generated = generated_objects(inputs, names) + slack_poster_tls_objects(inputs)
    documents = namespaces(directory) + kustomize(directory) + generated
    set_images(documents, inputs.build_id, inputs.image_registry)
    registry = f"{inputs.image_registry.rstrip('/')}/" if inputs.image_registry else ""
    set_validation_image(documents, f"{registry}{PLATFORM_IMAGE}:{inputs.build_id}")
    set_build_id(documents, inputs.build_id)
    add_bedrock_relay(documents, inputs.settings)
    tenants = local_tenants(names, documents, inputs.root)
    documents = localize_tenants(
        tenantize(documents, tenants),
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
    from scripts.env_file import platform_env_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--compose-file", action="append", default=[], dest="compose_files")
    parser.add_argument("--env-file", type=Path, default=platform_env_path())
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
