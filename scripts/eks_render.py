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
- `secrets`, each secret store key, and `temporal_ca`, mapped to its secret ARN
- `tenants`, each tenant's name, the volume handle of its workspace access point, its ticket store
  roles, its secret ARNs, the platform secret keys its worker reads and those its review worker
  reads, its settings, its model limits, its
  sandbox quota, the image prefixes its sandbox pods may run, the config bucket, and the host name
  of its MCP gateway, from which `scripts.tenant_render` renders its worker, review, and sandbox
  namespaces
- `vpc_cidr`, `service_cidr`, `gate_egress_cidrs`, and `gate_egress_internet`
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
# Secrets only a tenant holds, which no overlay provider lists because the secret store has no key
# for them. The worker of a tenant that holds one reads it as the upper-cased key.
TENANT_ONLY_SECRETS = (
    "repo_intelligence_token",
    "escalation_action_key",
    "slack_bot_token",
    "slack_app_token",
)
# The worker's secrets the tenant's MCP gateway also reads, for its Jira and GitHub readers and
# its repository intelligence calls.
GATEWAY_WORKER_SECRETS = ("github_token", "jira_api_token", "jira_pat", "repo_intelligence_token")
MODEL_LIMIT_ENV = {
    "monthly_budget_usd": "TENANT_MODEL_BUDGET_USD",
    "rpm_limit": "TENANT_MODEL_RPM_LIMIT",
    "tpm_limit": "TENANT_MODEL_TPM_LIMIT",
    "run_budget_usd": "RUN_MODEL_BUDGET_USD",
}


WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"})
PLATFORM_ESCALATION_FILE = "platform-escalation.yaml"


def require_platform_escalation(environment_config: Mapping[str, str]) -> None:
    """Refuse an environment config without platform-escalation.yaml, which the workers and the
    Slack posting service refuse to start without."""
    if PLATFORM_ESCALATION_FILE not in environment_config:
        raise RenderError(
            f"the environment config holds no {PLATFORM_ESCALATION_FILE}, and the workers and the "
            f"Slack posting service refuse to start without it. Copy "
            f"config/platform/{PLATFORM_ESCALATION_FILE}.example to {PLATFORM_ESCALATION_FILE} "
            "in container_config_dir, name the administrator destination, and apply"
        )


def workloads_last(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order documents so each workload applies after the Secrets and config its pods read.

    kubectl applies a list in order, and a workload whose pod template changed starts new pods
    at once, so a Secret or SecretProviderClass applied after it can miss those pods.
    """
    return sorted(documents, key=lambda document: document.get("kind") in WORKLOAD_KINDS)


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


def rebar_profiles(roles: Mapping[str, str]) -> str:
    return "\n".join(
        f"[profile rebar-tickets-{access}]\nrole_arn = {roles[access]}\n"
        "credential_source = EcsContainer\n"
        for access in ("reader", "writer")
    )


def tenant_workspace(
    documents: list[dict[str, Any]], tenant: str, volume_handle: str
) -> tuple[dict[str, Any], ...]:
    """Return the static volume and claim of a tenant's workspace access point."""
    from scripts.tenant_render import WORKSPACE_CLAIM, worker_namespace

    volume = next(d for d in documents if d.get("kind") == "PersistentVolume")
    claim = _named(documents, "PersistentVolumeClaim", WORKSPACE_CLAIM)
    if claim is None:
        raise RenderError("the overlay has no workspaces PersistentVolumeClaim")
    volume, claim = copy.deepcopy(volume), copy.deepcopy(claim)
    volume["metadata"]["name"] = f"tenant-{tenant}-workspace"
    volume["metadata"].setdefault("labels", {})["agentic-dev-platform/tenant"] = tenant
    volume["spec"]["csi"]["volumeHandle"] = volume_handle
    claim["metadata"]["namespace"] = worker_namespace(tenant)
    claim["spec"]["volumeName"] = volume["metadata"]["name"]
    return volume, claim


def tenant_review_workspace(
    documents: list[dict[str, Any]], tenant: str, volume_handle: str
) -> tuple[dict[str, Any], ...]:
    """Return a read-only volume and claim of the tenant's workspace for its review namespace.

    A claim binds one volume, so the review namespace's claim binds a second volume of the same
    access point. The EFS CSI driver refuses the ReadOnlyMany access mode, so the volume keeps the
    workspace's ReadWriteMany mode and csi.readOnly makes the driver mount it read-only.
    """
    from scripts.tenant_render import review_namespace

    volume, claim = tenant_workspace(documents, tenant, volume_handle)
    volume["metadata"]["name"] = f"tenant-{tenant}-review-workspace"
    volume["spec"]["csi"]["readOnly"] = True
    claim["metadata"]["namespace"] = review_namespace(tenant)
    claim["spec"]["volumeName"] = volume["metadata"]["name"]
    return volume, claim


def tenant_specs(documents: list[dict[str, Any]], inputs: Mapping[str, Any]) -> list[Any]:
    from scripts.tenant_render import Tenant, worker_env

    tenants = []
    for entry in inputs.get("tenants") or []:
        name = entry["name"]
        env = {**worker_env(name), **(entry.get("settings") or {})}
        limits = entry.get("model_limits") or {}
        env.update(
            {MODEL_LIMIT_ENV[key]: str(limits[key]) for key in MODEL_LIMIT_ENV if key in limits}
        )
        if entry.get("config_bucket"):
            env["TENANT_CONFIG_BUCKET"] = entry["config_bucket"]
        tenants.append(
            Tenant(
                name=name,
                env=env,
                workspace=tenant_workspace(documents, name, entry["workspace_volume_handle"]),
                review_workspace=tenant_review_workspace(
                    documents, name, entry["workspace_volume_handle"]
                ),
                config={"rebar-store-aws-config": {"config": rebar_profiles(entry["rebar_roles"])}},
                quota=entry.get("sandbox_quota") or {},
                image_prefixes=tuple(entry.get("image_prefixes") or ()),
            )
        )
    return tenants


def set_tenant_secrets(
    documents: list[dict[str, Any]],
    inputs: Mapping[str, Any],
    omit: Iterable[str],
) -> list[str]:
    """Point the platform providers at the platform secrets and each tenant's at its own.

    A provider of a tenant's worker or review namespace mounts the platform secrets its role may
    read, `platform_secrets` for the worker and `review_platform_secrets` for the review worker,
    and the tenant's own secrets it lists, and leaves out every other key. An omitted key is
    `<key>` for a platform secret and `<tenant>:<key>` for a tenant secret without a current value.
    """
    from scripts.tenant_render import review_namespace, worker_namespace

    omitted = set(omit)
    tenants = {
        namespace(entry["name"]): (entry, readable)
        for entry in inputs.get("tenants") or []
        for namespace, readable in (
            (worker_namespace, "platform_secrets"),
            (review_namespace, "review_platform_secrets"),
        )
    }
    platform = [d for d in documents if d.get("metadata", {}).get("namespace") not in tenants]
    skipped = set_secrets(platform, inputs["secrets"], {key for key in omitted if ":" not in key})
    for namespace, (entry, readable) in tenants.items():
        name = entry["name"]
        own = entry.get("secrets") or {}
        keys = entry.get(readable, entry.get("platform_secrets")) or []
        arns = {key: inputs["secrets"][key] for key in keys}
        arns.update(own)
        providers = [
            d
            for d in documents
            if d.get("kind") == "SecretProviderClass"
            and d["metadata"].get("namespace") == namespace
        ]
        add_tenant_only_secrets(providers, own)
        add_gateway_secrets(providers)
        mounted = {
            secret_key(item["objectName"]): item.get("objectAlias", "")
            for provider in providers
            for item in json.loads(provider["spec"]["parameters"]["objects"])
        }
        unset = {key.split(":", 1)[1] for key in omitted if key.startswith(f"{name}:")}
        unset |= {key for key in omitted if ":" not in key and key not in own}
        unset &= set(arns)
        set_secrets(providers, arns, (set(mounted) - set(arns)) | unset)
        skipped += [f"{namespace}:{mounted[key] or key}" for key in sorted(unset & set(mounted))]
    return skipped


def add_tenant_only_secrets(providers: list[dict[str, Any]], own: Mapping[str, str]) -> None:
    from scripts.tenant_render import WORKER

    for provider in providers:
        if provider["metadata"]["name"] != WORKER:
            continue
        objects = json.loads(provider["spec"]["parameters"]["objects"])
        listed = {secret_key(entry["objectName"]) for entry in objects}
        objects += [
            {
                "objectName": f"agentic-platform/tenant/{key.replace('_', '-')}",
                "objectType": "secretsmanager",
                "objectAlias": key.upper(),
            }
            for key in TENANT_ONLY_SECRETS
            if key in own and key not in listed
        ]
        provider["spec"]["parameters"]["objects"] = json.dumps(objects)


def add_gateway_secrets(providers: list[dict[str, Any]]) -> None:
    """Add the worker's tenant credentials the gateway reads to the gateway's provider."""
    from scripts.tenant_render import GATEWAY, WORKER

    named = {provider["metadata"]["name"]: provider for provider in providers}
    if GATEWAY not in named or WORKER not in named:
        return
    worker = json.loads(named[WORKER]["spec"]["parameters"]["objects"])
    objects = json.loads(named[GATEWAY]["spec"]["parameters"]["objects"])
    listed = {secret_key(entry["objectName"]) for entry in objects}
    objects += [
        copy.deepcopy(entry)
        for entry in worker
        if secret_key(entry["objectName"]) in GATEWAY_WORKER_SECRETS
        and secret_key(entry["objectName"]) not in listed
    ]
    named[GATEWAY]["spec"]["parameters"]["objects"] = json.dumps(objects)


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
    documents: list[dict[str, Any]],
    vpc_cidr: str,
    service_cidr: str,
    gate_cidrs: list[str],
    gate_internet: bool = False,
) -> None:
    """Point the policies at the environment's ranges.

    With `gate_internet`, which the aws registry mode sets, the gate pods also reach any address
    outside the VPC on 443, where DNS Firewall resolves only the allowed package registries.
    """
    for document in documents:
        if document.get("kind") != "NetworkPolicy":
            continue
        if document["metadata"]["name"] == GATE_EGRESS_POLICY:
            registry = next(r for r in document["spec"]["egress"] if _ports(r) == [443])
            registry["to"] = [{"ipBlock": {"cidr": cidr}} for cidr in gate_cidrs]
            if gate_internet:
                registry["to"].append({"ipBlock": {"cidr": "0.0.0.0/0", "except": [vpc_cidr]}})
            continue
        for rule in [*document["spec"].get("ingress", []), *document["spec"].get("egress", [])]:
            for peer in rule.get("from", []) + rule.get("to", []):
                block = peer.get("ipBlock")
                if block and block["cidr"] == SYNTHETIC_VPC_CIDR:
                    block["cidr"] = vpc_cidr
                elif block and block["cidr"] == SYNTHETIC_SERVICE_CIDR:
                    block["cidr"] = service_cidr


PLATFORM_BASELINE_POLICY = "platform-baseline"
DEFAULT_SMTP_PORT = 587


def set_smtp_egress(
    documents: list[dict[str, Any]], platform_environment: Mapping[str, Any]
) -> None:
    """Point the platform baseline's SMTP egress at SMTP_PORT, 587 unless the environment sets it.

    The workers, the inbound webhook, and the deployment-validate Job send alert email, and the
    tenant namespaces copy the platform baseline.
    """
    raw = str(platform_environment.get("SMTP_PORT") or DEFAULT_SMTP_PORT).strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise RenderError(f"SMTP_PORT {raw!r} of the platform environment is not a TCP port")
    for document in documents:
        if (
            document.get("kind") != "NetworkPolicy"
            or document["metadata"]["name"] != PLATFORM_BASELINE_POLICY
        ):
            continue
        smtp = next(r for r in document["spec"]["egress"] if _ports(r) == [DEFAULT_SMTP_PORT])
        smtp["ports"] = [{"protocol": "TCP", "port": int(raw)}]


def _ports(rule: Mapping[str, Any]) -> list[int]:
    return [port["port"] for port in rule.get("ports", [])]


def set_ingress(
    documents: list[dict[str, Any]], ingress: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Set each Ingress's certificate and the gateway's clients.

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
        kept.append(document)
    return kept


def gateway_hosts(inputs: Mapping[str, Any]) -> dict[str, str]:
    """Return the host name each tenant's MCP gateway clients connect to, by tenant."""
    return {
        entry["name"]: entry.get("mcp_gateway_host") or "" for entry in inputs.get("tenants") or []
    }


def set_gateway_hosts(documents: list[dict[str, Any]], hosts: Mapping[str, str]) -> None:
    """Name each tenant's gateway Ingress after its host, and admit only that host.

    A tenant whose gateway has no Ingress keeps an empty host list.
    """
    from scripts.tenant_render import GATEWAY, worker_namespace

    namespaces = {worker_namespace(tenant): (tenant, host) for tenant, host in hosts.items()}
    exposed: dict[str, str] = {}
    for document in documents:
        namespace = document["metadata"].get("namespace", "")
        if (
            document.get("kind") != "Ingress"
            or document["metadata"]["name"] != GATEWAY
            or namespace not in namespaces
        ):
            continue
        tenant, host = namespaces[namespace]
        if not host:
            raise RenderError(f"the {tenant} mcp-gateway Ingress has no host name")
        for rule in document["spec"]["rules"]:
            rule["host"] = host
        exposed[namespace] = host
    for document in documents:
        if document.get("kind") != "Deployment" or document["metadata"]["name"] != GATEWAY:
            continue
        host = exposed.get(document["metadata"].get("namespace", ""), "")
        for container in document["spec"]["template"]["spec"]["containers"]:
            for entry in container.get("env", []):
                if entry["name"] == "MCP_ALLOWED_HOSTS":
                    entry["value"] = f"{host},{host}:443" if host else ""


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
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the environment's manifests and the secret aliases left out of them."""
    from scripts.tenant_render import tenantize
    from scripts.validation_job import set_validation_image, set_validation_tenants
    from scripts.webhook_eks_render import (
        route_action_key_fingerprints,
        route_slack_poster,
        route_webhook_tenants,
        slack_poster_ca,
    )

    documents = copy.deepcopy(overlay_documents)
    require_platform_escalation(inputs.get("environment_config") or {})
    set_images(documents, inputs["images"])
    set_validation_image(documents, inputs["images"]["platform"])
    set_validation_tenants(documents, [entry["name"] for entry in inputs.get("tenants") or []])
    set_settings(documents, inputs)
    set_security_groups(documents, inputs["security_groups"])
    set_network_cidrs(
        documents,
        inputs["vpc_cidr"],
        inputs["service_cidr"],
        inputs["gate_egress_cidrs"],
        bool(inputs.get("gate_egress_internet")),
    )
    set_smtp_egress(documents, inputs.get("platform_environment") or {})
    documents = set_ingress(documents, inputs.get("ingress") or {})
    generated = generated_objects(inputs, root)
    tenants = tenant_specs(documents, inputs)
    if not tenants:
        raise RenderError("the inputs name no tenant, and every activity worker serves one")
    generated += slack_poster_ca(inputs)
    documents = route_slack_poster(tenantize(documents + generated, tenants), inputs)
    set_gateway_hosts(documents, gateway_hosts(inputs))
    omit_secrets = list(omit_secrets)
    skipped = set_tenant_secrets(documents, inputs, omit_secrets)
    skipped += route_webhook_tenants(documents, inputs, omit_secrets)
    route_action_key_fingerprints(documents, inputs)
    stamp_config_hash(documents, [])
    return documents, skipped
