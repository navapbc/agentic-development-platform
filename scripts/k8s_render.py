"""Render the local cluster's manifests with the generated config, secrets, and build image.

The kustomize overlay holds the static shape. This adds what is derived from the workstation
at deploy time: the platform settings from `.env` with the compose addresses rewritten for the
pods, the operator config files, the promotion policies, the LiteLLM config, the image of the
current build, the Bedrock tunnel relay when the connectivity provider needs one, each tenant's
worker and sandbox namespaces, and the namespaces the settings name.

Each tenant's workspace is a hostPath directory on the node under TENANTS_HOST_ROOT, and the
review worker mounts that root read-only at /tenants, the layout the EFS access points give the
workers in AWS.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from services import process

from scripts.cluster_env import (
    ComposeNetwork,
    endpoint_values,
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
    return [
        secret("agentic-platform-env", rewrite_env(inputs.env, network, address).values),
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


def render(inputs: RenderInputs, overlay: Path = OVERLAY) -> list[dict[str, Any]]:
    """Return the namespaces, then the overlay, then the generated config, ready to apply."""
    from scripts.tenant_render import tenantize

    directory = inputs.root / overlay
    generated = generated_objects(inputs)
    documents = namespaces(directory) + kustomize(directory) + generated
    set_images(documents, inputs.build_id, inputs.image_registry)
    set_build_id(documents, inputs.build_id)
    add_bedrock_relay(documents, inputs.settings)
    documents = tenantize(
        mount_tenants_on_review(documents),
        local_tenants(inputs.settings, documents, inputs.root),
    )
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
