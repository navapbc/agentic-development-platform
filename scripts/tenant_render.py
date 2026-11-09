"""Give each tenant its own activity worker namespace and sandbox namespace.

The overlays describe one activity worker in the platform namespace and one sandbox namespace.
This turns that shape into a template. For each tenant it renders tenant-<tenant>, holding a copy
of the worker with its ServiceAccount, secret provider, config, volume claim, security group
policy, and network policies, and tenant-<tenant>-sandbox, holding a copy of the sandbox
namespace's Role and network policies, with a ResourceQuota when the tenant sets one. The
platform namespace keeps LiteLLM, the webhook, the gateway, and the shared config, and admits the
tenant namespaces by label. The original worker and sandbox namespace are not rendered, and
RETIRED lists them for the deployers to delete, because an apply does not prune.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

PLATFORM_NAMESPACE = "agentic-dev-platform"
SANDBOX_NAMESPACE = "agent-sandbox"
WORKER = "activity-worker"
WORKSPACE_CLAIM = "workspaces"
NAME_LABEL = "app.kubernetes.io/name"
PART_OF_LABEL = "app.kubernetes.io/part-of"
TENANT_LABEL = "agentic-dev-platform/tenant"
ROLE_LABEL = "agentic-dev-platform/namespace-role"
WORKER_ROLE = "worker"
SANDBOX_ROLE = "sandbox"

# Objects a deployment rendered before tenants, which the deployers delete by kind and name.
RETIRED = (
    ("deployment", PLATFORM_NAMESPACE, WORKER),
    ("serviceaccount", PLATFORM_NAMESPACE, WORKER),
    ("secretproviderclass", PLATFORM_NAMESPACE, WORKER),
    ("persistentvolumeclaim", PLATFORM_NAMESPACE, WORKSPACE_CLAIM),
    ("persistentvolume", "", "agentic-platform-checkouts"),
    ("configmap", PLATFORM_NAMESPACE, "rebar-store-aws-config"),
    ("configmap", PLATFORM_NAMESPACE, "promotion-policies"),
    ("namespace", "", SANDBOX_NAMESPACE),
)


@dataclass(frozen=True)
class Tenant:
    name: str
    env: Mapping[str, str] = field(default_factory=dict)
    workspace: tuple[dict[str, Any], ...] = ()
    config: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    quota: Mapping[str, str] = field(default_factory=dict)
    init_containers: tuple[dict[str, Any], ...] = ()

    @property
    def namespace(self) -> str:
        return worker_namespace(self.name)

    @property
    def sandbox_namespace(self) -> str:
        return sandbox_namespace(self.name)

    @property
    def mount_path(self) -> str:
        return tenant_root(self.name)


def worker_namespace(tenant: str) -> str:
    return f"tenant-{tenant}"


def sandbox_namespace(tenant: str) -> str:
    return f"tenant-{tenant}-sandbox"


def tenant_root(tenant: str) -> str:
    return f"/tenants/{tenant}"


def worker_env(tenant: str) -> dict[str, str]:
    """Return the settings that place a tenant's worker on its namespaces and paths."""
    root = tenant_root(tenant)
    return {
        "TENANT": tenant,
        "OPENHANDS_SANDBOX_NAMESPACE": sandbox_namespace(tenant),
        "GATE_ENGINE_NAMESPACE": sandbox_namespace(tenant),
        "REPOSITORY_ROOT": f"{root}/repositories",
        "EXECUTION_WORKSPACE_ROOT": f"{root}/workspaces",
        "GATE_ENGINE_ROOT": f"{root}/gate-engines",
    }


def _pod_spec(document: Mapping[str, Any]) -> dict[str, Any] | None:
    if document.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        return document["spec"]["template"]["spec"]
    return None


def _key(document: Mapping[str, Any]) -> tuple[str, str, str]:
    metadata = document["metadata"]
    return document["kind"], metadata.get("namespace", ""), metadata["name"]


def _find(documents: Iterable[dict[str, Any]], kind: str, namespace: str, name: str):
    return next((d for d in documents if _key(d) == (kind, namespace, name)), None)


def _referenced_config(spec: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Return the ConfigMaps and Secrets the pod spec reads, by kind and name."""
    found: set[tuple[str, str]] = set()
    for container in [*spec.get("initContainers", []), *spec.get("containers", [])]:
        for source in container.get("envFrom", []):
            if "configMapRef" in source:
                found.add(("ConfigMap", source["configMapRef"]["name"]))
            if "secretRef" in source:
                found.add(("Secret", source["secretRef"]["name"]))
        for variable in container.get("env", []):
            reference = variable.get("valueFrom") or {}
            if "configMapKeyRef" in reference:
                found.add(("ConfigMap", reference["configMapKeyRef"]["name"]))
            if "secretKeyRef" in reference:
                found.add(("Secret", reference["secretKeyRef"]["name"]))
    for volume in spec.get("volumes", []):
        if "configMap" in volume:
            found.add(("ConfigMap", volume["configMap"]["name"]))
        if "secret" in volume:
            found.add(("Secret", volume["secret"]["secretName"]))
        for source in (volume.get("projected") or {}).get("sources", []):
            if "configMap" in source:
                found.add(("ConfigMap", source["configMap"]["name"]))
            if "secret" in source:
                found.add(("Secret", source["secret"]["name"]))
    return found


def _secret_provider(spec: Mapping[str, Any]) -> str:
    for volume in spec.get("volumes", []):
        attributes = (volume.get("csi") or {}).get("volumeAttributes") or {}
        if attributes.get("secretProviderClass"):
            return attributes["secretProviderClass"]
    return ""


def _selects_worker(selector: Mapping[str, Any] | None) -> bool:
    if selector is None:
        return False
    labels = selector.get("matchLabels") or {}
    if labels.get(NAME_LABEL) == WORKER:
        return True
    for expression in selector.get("matchExpressions") or []:
        if expression.get("key") == NAME_LABEL and WORKER in expression.get("values", []):
            return True
    return not labels and not selector.get("matchExpressions")


def _namespace_named(peer: Mapping[str, Any]) -> str:
    labels = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
    return labels.get("kubernetes.io/metadata.name", "")


def _select_namespace(peer: dict[str, Any], namespace: str) -> None:
    peer["namespaceSelector"] = {"matchLabels": {"kubernetes.io/metadata.name": namespace}}


def _tenant_namespaces_selector(role: str | None = None) -> dict[str, Any]:
    expressions = [{"key": TENANT_LABEL, "operator": "Exists"}]
    if role is not None:
        expressions.append({"key": ROLE_LABEL, "operator": "In", "values": [role]})
    return {"matchExpressions": expressions}


def _peers(policy: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    spec = policy["spec"]
    for rule in [*spec.get("ingress", []), *spec.get("egress", [])]:
        yield from rule.get("from", [])
        yield from rule.get("to", [])


def _worker_policy(policy: dict[str, Any], tenant: Tenant) -> dict[str, Any]:
    """Copy a platform policy that selects the worker into the tenant's worker namespace."""
    policy = copy.deepcopy(policy)
    policy["metadata"]["namespace"] = tenant.namespace
    for peer in _peers(policy):
        named = _namespace_named(peer)
        if named == SANDBOX_NAMESPACE:
            _select_namespace(peer, tenant.sandbox_namespace)
        elif (
            not named
            and "namespaceSelector" not in peer
            and peer.get("podSelector")
            and not _selects_worker(peer["podSelector"])
        ):
            _select_namespace(peer, PLATFORM_NAMESPACE)
    return policy


def _sandbox_policy(policy: dict[str, Any], tenant: Tenant) -> dict[str, Any]:
    """Copy a sandbox namespace policy, pointing its worker peers at the tenant's worker."""
    policy = copy.deepcopy(policy)
    policy["metadata"]["namespace"] = tenant.sandbox_namespace
    for peer in _peers(policy):
        if (
            _namespace_named(peer) == PLATFORM_NAMESPACE
            and peer.get("podSelector")
            and _selects_worker(peer["podSelector"])
        ):
            _select_namespace(peer, tenant.namespace)
    return policy


def _platform_policy(policy: dict[str, Any]) -> dict[str, Any] | None:
    """Admit the tenant namespaces where a platform policy admitted the sandbox namespace.

    A rule that reached the sandbox namespace from the platform namespace is dropped, because no
    platform pod but the worker talked to the sandboxes, and the worker has moved.
    """
    policy = copy.deepcopy(policy)
    spec = policy["spec"]
    for direction, side in (("ingress", "from"), ("egress", "to")):
        rules = []
        for rule in spec.get(direction, []):
            peers = []
            for peer in rule.get(side, []):
                if _namespace_named(peer) != SANDBOX_NAMESPACE:
                    peers.append(peer)
                elif direction == "ingress":
                    peers.append({"namespaceSelector": _tenant_namespaces_selector()})
            if side in rule and not peers:
                continue
            if side in rule:
                rule = {**rule, side: peers}
            rules.append(rule)
        if direction in spec:
            spec[direction] = rules
    return policy


def _fqdn_services(value: str, services: Iterable[str]) -> str:
    for service in services:
        value = re.sub(
            rf"(://){re.escape(service)}(:\d+|/|$)",
            rf"\g<1>{service}.{PLATFORM_NAMESPACE}.svc.cluster.local\g<2>",
            value,
        )
    return value


def _tenant_worker(deployment: dict[str, Any], tenant: Tenant, services: list[str]) -> dict:
    worker = copy.deepcopy(deployment)
    worker["metadata"]["namespace"] = tenant.namespace
    worker["metadata"].setdefault("labels", {})[TENANT_LABEL] = tenant.name
    template = worker["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("labels", {})[TENANT_LABEL] = tenant.name
    spec = template["spec"]
    spec["initContainers"] = [*tenant.init_containers, *spec.get("initContainers", [])]
    for container in spec["containers"]:
        env = [
            {**variable, "value": _fqdn_services(variable["value"], services)}
            if "value" in variable
            else variable
            for variable in container.get("env", [])
            if variable["name"] not in tenant.env
        ]
        env += [{"name": name, "value": value} for name, value in sorted(tenant.env.items())]
        container["env"] = env
        for mount in container.get("volumeMounts", []):
            if mount["name"] == WORKSPACE_CLAIM:
                mount["mountPath"] = tenant.mount_path
    return worker


def _namespace(name: str, tenant: str, role: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {
                PART_OF_LABEL: PLATFORM_NAMESPACE,
                TENANT_LABEL: tenant,
                ROLE_LABEL: role,
            },
        },
    }


def _quota(tenant: Tenant) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "sandbox", "namespace": tenant.sandbox_namespace},
        "spec": {"hard": dict(tenant.quota)},
    }


def tenantize(documents: list[dict[str, Any]], tenants: Iterable[Tenant]) -> list[dict[str, Any]]:
    """Render each tenant's namespaces from the platform worker and the sandbox namespace."""
    tenants = list(tenants)
    deployment = _find(documents, "Deployment", PLATFORM_NAMESPACE, WORKER)
    if deployment is None or not tenants:
        return documents
    spec = _pod_spec(deployment)
    config = _referenced_config(spec)
    provider = _secret_provider(spec)
    services = [name for kind, namespace, name in map(_key, documents) if kind == "Service"]
    shared = set().union(
        *(
            _referenced_config(_pod_spec(d))
            for d in documents
            if _pod_spec(d) is not None
            and d["metadata"].get("namespace") == PLATFORM_NAMESPACE
            and d["metadata"]["name"] != WORKER
        )
    )
    worker_only = config - shared
    claim = _find(documents, "PersistentVolumeClaim", PLATFORM_NAMESPACE, WORKSPACE_CLAIM)
    volume = (claim or {}).get("spec", {}).get("volumeName", "")

    def retired(document: dict[str, Any]) -> bool:
        kind, namespace, name = _key(document)
        if namespace == SANDBOX_NAMESPACE or (kind == "Namespace" and name == SANDBOX_NAMESPACE):
            return True
        if kind == "PersistentVolume" and volume and name == volume:
            return True
        if namespace == PLATFORM_NAMESPACE and (kind, name) in worker_only:
            return True
        return namespace == PLATFORM_NAMESPACE and (kind, name) in {
            ("Deployment", WORKER),
            ("ServiceAccount", WORKER),
            ("SecretProviderClass", provider),
            ("PersistentVolumeClaim", WORKSPACE_CLAIM),
        }

    platform = []
    for document in documents:
        if retired(document):
            continue
        if document["kind"] == "NetworkPolicy" and document["metadata"].get("namespace") == (
            PLATFORM_NAMESPACE
        ):
            document = _platform_policy(document)
        platform.append(document)

    rendered = list(platform)
    for tenant in tenants:
        rendered += [
            _namespace(tenant.namespace, tenant.name, WORKER_ROLE),
            _namespace(tenant.sandbox_namespace, tenant.name, SANDBOX_ROLE),
        ]
        for document in documents:
            kind, namespace, name = _key(document)
            if namespace == SANDBOX_NAMESPACE:
                copied = (
                    _sandbox_policy(document, tenant)
                    if kind == "NetworkPolicy"
                    else copy.deepcopy(document)
                )
                copied["metadata"]["namespace"] = tenant.sandbox_namespace
                for subject in copied.get("subjects", []):
                    if subject.get("namespace") == PLATFORM_NAMESPACE:
                        subject["namespace"] = tenant.namespace
                rendered.append(copied)
            elif namespace != PLATFORM_NAMESPACE:
                continue
            elif (kind, name) in config or (kind, name) in {
                ("ServiceAccount", WORKER),
                ("SecretProviderClass", provider),
            }:
                copied = copy.deepcopy(document)
                copied["metadata"]["namespace"] = tenant.namespace
                if kind == "ConfigMap" and name in tenant.config:
                    copied["data"] = dict(tenant.config[name])
                rendered.append(copied)
            elif kind == "NetworkPolicy" and _selects_worker(document["spec"].get("podSelector")):
                rendered.append(_worker_policy(document, tenant))
            elif kind == "SecurityGroupPolicy" and _selects_worker(
                document["spec"].get("podSelector")
            ):
                copied = copy.deepcopy(document)
                copied["metadata"]["namespace"] = tenant.namespace
                copied["spec"]["podSelector"] = {"matchLabels": {NAME_LABEL: WORKER}}
                rendered.append(copied)
        rendered.append(_tenant_worker(deployment, tenant, services))
        for document in tenant.workspace:
            rendered.append(copy.deepcopy(document))
        if tenant.quota:
            rendered.append(_quota(tenant))
    return sorted(rendered, key=lambda document: document["kind"] != "Namespace")
