"""Turn the rendered AWS overlay into the manifests for one environment.

The `deploy/kubernetes/aws` overlay carries synthetic per-environment values. This replaces them
with the values the `private-bedrock` composition root records for the environment, and adds the
ConfigMaps the overlay only references: the platform settings, the environment's config files,
the LiteLLM configuration, and the promotion policies. It runs inside the eks-deployer task, so it
uses only the standard library and works on manifests already parsed into dictionaries.

The inputs are the JSON the eks-deployer task definition carries in DEPLOY_INPUTS:

- `images`, the platform, OPA, and LiteLLM image references
- `image_settings` and `settings`, values for the `agentic-platform-eks` ConfigMap
- `platform_environment` and `environment_config`, for `agentic-platform-env` and `platform-config`
- `security_groups`, the cluster, Temporal client, and internal client group IDs
- `workspace_volume_handle`, `<file system>::<access point>` of the checkouts access point
- `rebar_roles`, the reader and writer role ARNs of the ticket store
- `secrets`, each secret store key, and `temporal_ca`, mapped to its secret ARN
- `vpc_cidr`, `service_cidr`, and `gate_egress_cidrs`
- `ingress`, the webhook and gateway certificate ARNs and the gateway client CIDRs
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

PLATFORM_NAMESPACE = "agentic-dev-platform"
CONFIG_HASH = "agentic-dev-platform/config-hash"
SYNTHETIC_REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com/agentic-platform/"
SYNTHETIC_VPC_CIDR = "10.0.0.0/16"
SYNTHETIC_SERVICE_CIDR = "172.20.0.0/16"
IMAGE_KEYS = {"platform": "platform", "opa": "opa", "litellm": "litellm"}
SECURITY_GROUP_POLICIES = {
    "temporal-clients": ("cluster", "temporal_client", "internal_client"),
    "internal-clients": ("cluster", "internal_client"),
}
GATE_EGRESS_POLICY = "gate-engine-egress"
LITELLM_FILES = ("litellm-config.yaml", "litellm_redaction.py")


class RenderError(Exception):
    """The inputs cannot produce the manifests."""


def _named(documents: Iterable[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    return next(
        (d for d in documents if d.get("kind") == kind and d["metadata"]["name"] == name), None
    )


def _pod_spec(document: Mapping[str, Any]) -> dict[str, Any] | None:
    if document.get("kind") != "Deployment":
        return None
    return document["spec"]["template"]["spec"]


def config_map(name: str, data: Mapping[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": PLATFORM_NAMESPACE,
            "labels": {"app.kubernetes.io/part-of": PLATFORM_NAMESPACE},
        },
        "data": dict(data),
    }


def secret_key(object_name: str) -> str:
    """Return the secret store key of an overlay object name, `<prefix>/<group>/<name>`."""
    _, group, name = object_name.split("/")
    if group == "temporal":
        return f"temporal_{name.removeprefix('tls-').replace('-', '_')}"
    return ("database_" if group == "database" else "") + name.replace("-", "_")


def set_images(documents: list[dict[str, Any]], images: Mapping[str, str]) -> None:
    for document in documents:
        spec = _pod_spec(document)
        if spec is None:
            continue
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            image = container["image"]
            if not image.startswith(SYNTHETIC_REGISTRY):
                raise RenderError(
                    f"{container['name']} names {image}, outside the overlay registry"
                )
            repository = image.removeprefix(SYNTHETIC_REGISTRY).split(":", 1)[0].split("@", 1)[0]
            if repository not in IMAGE_KEYS or not images.get(IMAGE_KEYS[repository]):
                raise RenderError(f"no published image for {repository}")
            container["image"] = images[IMAGE_KEYS[repository]]


def set_settings(documents: list[dict[str, Any]], inputs: Mapping[str, Any]) -> None:
    settings = _named(documents, "ConfigMap", "agentic-platform-eks")
    if settings is None:
        raise RenderError("the overlay has no agentic-platform-eks ConfigMap")
    settings["data"].update(inputs.get("image_settings") or {})
    settings["data"].update(inputs.get("settings") or {})
    synthetic = sorted(k for k, v in settings["data"].items() if v.startswith(SYNTHETIC_REGISTRY))
    if synthetic:
        raise RenderError(f"no published image for {', '.join(synthetic)}")


def set_rebar_roles(documents: list[dict[str, Any]], roles: Mapping[str, str]) -> None:
    profiles = _named(documents, "ConfigMap", "rebar-store-aws-config")
    if profiles is None:
        raise RenderError("the overlay has no rebar-store-aws-config ConfigMap")
    profiles["data"]["config"] = "\n".join(
        f"[profile rebar-tickets-{access}]\nrole_arn = {roles[access]}\n"
        "credential_source = EcsContainer\n"
        for access in ("reader", "writer")
    )


def set_workspace(documents: list[dict[str, Any]], volume_handle: str) -> None:
    for document in documents:
        if document.get("kind") == "PersistentVolume":
            document["spec"]["csi"]["volumeHandle"] = volume_handle


def set_security_groups(documents: list[dict[str, Any]], groups: Mapping[str, str]) -> None:
    for name, keys in SECURITY_GROUP_POLICIES.items():
        policy = _named(documents, "SecurityGroupPolicy", name)
        if policy is None:
            raise RenderError(f"the overlay has no {name} SecurityGroupPolicy")
        policy["spec"]["securityGroups"]["groupIds"] = [groups[key] for key in keys]


def set_secrets(
    documents: list[dict[str, Any]], arns: Mapping[str, str], omit: Iterable[str] = ()
) -> list[str]:
    """Point each SecretProviderClass object at its ARN, leaving out the keys in `omit`.

    Returns the omitted aliases, because a pod whose provider names a secret without a current
    value never starts, and the caller reports what each workload runs without.
    """
    skipped: list[str] = []
    omitted = set(omit)
    for document in documents:
        if document.get("kind") != "SecretProviderClass":
            continue
        objects = json.loads(document["spec"]["parameters"]["objects"])
        kept = []
        for entry in objects:
            key = secret_key(entry["objectName"])
            if key in omitted:
                skipped.append(f"{document['metadata']['name']}:{entry.get('objectAlias', key)}")
                continue
            if key not in arns:
                raise RenderError(f"no secret ARN for {key}")
            entry["objectName"] = arns[key]
            kept.append(entry)
        document["spec"]["parameters"]["objects"] = json.dumps(kept)
    return skipped


def set_network_cidrs(
    documents: list[dict[str, Any]], vpc_cidr: str, service_cidr: str, gate_cidrs: list[str]
) -> None:
    for document in documents:
        if document.get("kind") != "NetworkPolicy":
            continue
        if document["metadata"]["name"] == GATE_EGRESS_POLICY:
            registry = next(r for r in document["spec"]["egress"] if _ports(r) == [443])
            registry["to"] = [{"ipBlock": {"cidr": cidr}} for cidr in gate_cidrs]
            continue
        for rule in [*document["spec"].get("ingress", []), *document["spec"].get("egress", [])]:
            for peer in rule.get("from", []) + rule.get("to", []):
                block = peer.get("ipBlock")
                if block and block["cidr"] == SYNTHETIC_VPC_CIDR:
                    block["cidr"] = vpc_cidr
                elif block and block["cidr"] == SYNTHETIC_SERVICE_CIDR:
                    block["cidr"] = service_cidr


def _ports(rule: Mapping[str, Any]) -> list[int]:
    return [port["port"] for port in rule.get("ports", [])]


def set_ingress(
    documents: list[dict[str, Any]], ingress: Mapping[str, Any], mcp_hostname: str = ""
) -> list[dict[str, Any]]:
    """Set each Ingress's certificate and the gateway's clients and host list.

    An Ingress whose certificate the environment has not set is left out, because the controller
    refuses to reconcile one whose certificate does not resolve.
    """
    certificates = {
        "inbound-webhook": ingress.get("webhook_certificate_arn") or "",
        "mcp-gateway": ingress.get("mcp_gateway_certificate_arn") or "",
    }
    kept = []
    for document in documents:
        if document.get("kind") == "Ingress":
            name = document["metadata"]["name"]
            if not certificates.get(name):
                continue
            annotations = document["metadata"]["annotations"]
            annotations["alb.ingress.kubernetes.io/certificate-arn"] = certificates[name]
            if name == "mcp-gateway":
                cidrs = ingress.get("mcp_gateway_client_cidrs") or []
                annotations["alb.ingress.kubernetes.io/inbound-cidrs"] = ",".join(cidrs)
        if document.get("kind") == "Deployment" and document["metadata"]["name"] == "mcp-gateway":
            for container in document["spec"]["template"]["spec"]["containers"]:
                for entry in container.get("env", []):
                    if entry["name"] == "MCP_ALLOWED_HOSTS":
                        entry["value"] = (
                            f"{mcp_hostname},{mcp_hostname}:443" if mcp_hostname else ""
                        )
        kept.append(document)
    return kept


def generated_objects(inputs: Mapping[str, Any], root: Path) -> list[dict[str, Any]]:
    policies = root / "config" / "policies"
    return [
        config_map("agentic-platform-env", inputs.get("platform_environment") or {}),
        config_map("platform-config", inputs.get("environment_config") or {}),
        config_map(
            "litellm-config",
            {
                name: (root / "config" / "platform" / name).read_text(encoding="utf-8")
                for name in LITELLM_FILES
            },
        ),
        config_map(
            "promotion-policies",
            {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted(policies.glob("*.rego"))
            },
        ),
    ]


def stamp_config_hash(documents: list[dict[str, Any]], generated: list[dict[str, Any]]) -> None:
    """Annotate each pod template with a digest of its config, so a config change rolls it."""
    config = json.dumps(
        [d["data"] for d in generated]
        + [
            d.get("spec", d.get("data"))
            for d in documents
            if d.get("kind") in ("ConfigMap", "SecretProviderClass")
        ],
        sort_keys=True,
    )
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


def render(
    overlay_documents: list[dict[str, Any]],
    inputs: Mapping[str, Any],
    root: Path,
    *,
    omit_secrets: Iterable[str] = (),
    mcp_hostname: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the environment's manifests and the secret aliases left out of them."""
    documents = copy.deepcopy(overlay_documents)
    set_images(documents, inputs["images"])
    set_settings(documents, inputs)
    set_rebar_roles(documents, inputs["rebar_roles"])
    set_workspace(documents, inputs["workspace_volume_handle"])
    set_security_groups(documents, inputs["security_groups"])
    skipped = set_secrets(documents, inputs["secrets"], omit_secrets)
    set_network_cidrs(
        documents, inputs["vpc_cidr"], inputs["service_cidr"], inputs["gate_egress_cidrs"]
    )
    documents = set_ingress(documents, inputs.get("ingress") or {}, mcp_hostname)
    generated = generated_objects(inputs, root)
    stamp_config_hash(documents, generated)
    return documents + generated, skipped
