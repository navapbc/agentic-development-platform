"""Give each tenant its own activity worker namespace and sandbox namespace.

The overlays describe one activity worker in the platform namespace and one sandbox namespace.
This turns that shape into a template. For each tenant it renders tenant-<tenant>, holding a copy
of the worker with its ServiceAccount, secret provider, config, volume claim, security group
policy, and network policies, and tenant-<tenant>-sandbox, holding a copy of the sandbox
namespace's Role and network policies, with a ResourceQuota when the tenant sets one. The
platform namespace keeps LiteLLM, the webhook, and the shared config, and admits the tenant
namespaces by label. The original worker and sandbox namespace are not rendered, and RETIRED lists
them for the deployers to delete, because an apply does not prune.

The MCP gateway moves into the tenant namespaces the same way. Each tenant's gateway runs as its
worker's ServiceAccount, whose identity alone reads the tenant's secrets, and reads the worker's
settings and config in place of every platform ConfigMap and Secret the platform gateway read. It
keeps its own secret provider, Service, Ingress, and the platform network and security group
policies that select it by name.

The review worker moves into tenant-<tenant>-review the same way, one copy per tenant with its
ServiceAccount, the config it reads, and the platform policies that select it by name. It reads only
its own tenant's workspace, and the sandbox namespace binds its ServiceAccount to the sandbox Role,
so it starts the tenant's gate and scanner pods in the tenant's sandbox namespace only.
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
GATEWAY = "mcp-gateway"
REVIEW_WORKER = "review-worker"
WORKSPACE_CLAIM = "workspaces"
NAME_LABEL = "app.kubernetes.io/name"
PART_OF_LABEL = "app.kubernetes.io/part-of"
TENANT_LABEL = "agentic-dev-platform/tenant"
ROLE_LABEL = "agentic-dev-platform/namespace-role"
WORKER_ROLE = "worker"
SANDBOX_ROLE = "sandbox"
REVIEW_ROLE = "review"

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
    ("deployment", PLATFORM_NAMESPACE, GATEWAY),
    ("service", PLATFORM_NAMESPACE, GATEWAY),
    ("serviceaccount", PLATFORM_NAMESPACE, GATEWAY),
    ("ingress", PLATFORM_NAMESPACE, GATEWAY),
    ("secretproviderclass", PLATFORM_NAMESPACE, GATEWAY),
    ("deployment", PLATFORM_NAMESPACE, REVIEW_WORKER),
    ("serviceaccount", PLATFORM_NAMESPACE, REVIEW_WORKER),
)


@dataclass(frozen=True)
class Tenant:
    name: str
    env: Mapping[str, str] = field(default_factory=dict)
    workspace: tuple[dict[str, Any], ...] = ()
    config: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    quota: Mapping[str, str] = field(default_factory=dict)
    init_containers: tuple[dict[str, Any], ...] = ()
    image_prefixes: tuple[str, ...] = ()
    review_workspace: tuple[dict[str, Any], ...] = ()

    @property
    def namespace(self) -> str:
        return worker_namespace(self.name)

    @property
    def review_namespace(self) -> str:
        return review_namespace(self.name)

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


def review_namespace(tenant: str) -> str:
    return f"tenant-{tenant}-review"


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


def _names(selector: Mapping[str, Any] | None, name: str) -> bool:
    """Return whether the selector picks the workload by its name label."""
    if selector is None:
        return False
    if (selector.get("matchLabels") or {}).get(NAME_LABEL) == name:
        return True
    return any(
        expression.get("key") == NAME_LABEL
        and expression.get("operator") == "In"
        and name in expression.get("values", [])
        for expression in selector.get("matchExpressions") or []
    )


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
    return _tenant_workload(deployment, tenant, tenant.namespace, services, tenant.init_containers)


def _tenant_review_worker(
    deployment: dict[str, Any], tenant: Tenant, services: list[str]
) -> dict[str, Any]:
    return _tenant_workload(deployment, tenant, tenant.review_namespace, services, ())


def _tenant_workload(
    deployment: dict[str, Any],
    tenant: Tenant,
    namespace: str,
    services: list[str],
    init_containers: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    worker = copy.deepcopy(deployment)
    worker["metadata"]["namespace"] = namespace
    worker["metadata"].setdefault("labels", {})[TENANT_LABEL] = tenant.name
    template = worker["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("labels", {})[TENANT_LABEL] = tenant.name
    spec = template["spec"]
    spec["initContainers"] = [*init_containers, *spec.get("initContainers", [])]
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


def _volume_config(volume: Mapping[str, Any]) -> set[tuple[str, str]]:
    return _referenced_config({"volumes": [volume]})


def _variable_config(variable: Mapping[str, Any]) -> set[tuple[str, str]]:
    return _referenced_config({"containers": [{"env": [variable]}]})


def _tenant_gateway(
    gateway: dict[str, Any],
    worker: Mapping[str, Any],
    tenant: Tenant,
    copied: set[tuple[str, str]],
    services: list[str],
) -> dict[str, Any]:
    """Copy the platform gateway into the tenant's namespace, reading the tenant's worker config.

    A variable or volume of the platform gateway that reads a ConfigMap or Secret the tenant's
    namespace does not hold is replaced by the worker's settings, the worker's volume at the same
    mount path, or nothing.
    """
    gateway = copy.deepcopy(gateway)
    gateway["metadata"]["namespace"] = tenant.namespace
    gateway["metadata"].setdefault("labels", {})[TENANT_LABEL] = tenant.name
    template = gateway["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("labels", {})[TENANT_LABEL] = tenant.name
    spec = template["spec"]
    worker_spec = worker["spec"]["template"]["spec"]
    spec["serviceAccountName"] = worker_spec["serviceAccountName"]
    container = next(c for c in worker_spec["containers"] if c["name"] == WORKER)
    worker_volumes = {volume["name"]: volume for volume in worker_spec.get("volumes", [])}
    worker_mounts = {mount["mountPath"]: mount for mount in container.get("volumeMounts", [])}
    own_volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
    volumes: dict[str, dict[str, Any]] = {}
    for gateway_container in spec["containers"]:
        sources = [
            source
            for source in gateway_container.get("envFrom", [])
            if _referenced_config({"containers": [{"envFrom": [source]}]}) <= copied
        ]
        sources += [source for source in container.get("envFrom", []) if source not in sources]
        gateway_container["envFrom"] = copy.deepcopy(sources)
        env = [
            {**variable, "value": _fqdn_services(variable["value"], services)}
            if "value" in variable
            else variable
            for variable in gateway_container.get("env", [])
            if variable["name"] not in tenant.env and _variable_config(variable) <= copied
        ]
        env += [{"name": name, "value": value} for name, value in sorted(tenant.env.items())]
        gateway_container["env"] = env
        mounts = []
        for mount in gateway_container.get("volumeMounts", []):
            volume = own_volumes[mount["name"]]
            if _volume_config(volume) <= copied:
                mounts.append(mount)
                volumes[volume["name"]] = volume
            elif mount["mountPath"] in worker_mounts:
                shared = copy.deepcopy(worker_mounts[mount["mountPath"]])
                mounts.append(shared)
                volumes[shared["name"]] = copy.deepcopy(worker_volumes[shared["name"]])
        gateway_container["volumeMounts"] = mounts
    spec["volumes"] = list(volumes.values())
    return gateway


def _gateway_policy(
    policy: dict[str, Any], tenant: Tenant, gateway: Mapping[str, Any]
) -> dict[str, Any]:
    """Copy a platform policy that admits the gateway by name, for the tenant's gateway only.

    Ingress ports the gateway does not listen on, such as the webhook's, are dropped, and so is
    a rule left with no port, since a rule without ports admits every port.
    """
    policy = _worker_policy(policy, tenant)
    policy["metadata"]["name"] = f"{GATEWAY}-{policy['metadata']['name']}"
    policy["spec"]["podSelector"] = {"matchLabels": {NAME_LABEL: GATEWAY}}
    listening = {
        port[key]
        for container in gateway["spec"]["template"]["spec"]["containers"]
        for port in container.get("ports", [])
        for key in ("containerPort", "name")
        if key in port
    }
    rules = []
    for rule in policy["spec"].get("ingress", []):
        if "ports" in rule:
            rule["ports"] = [port for port in rule["ports"] if port.get("port") in listening]
            if not rule["ports"]:
                continue
        rules.append(rule)
    if "ingress" in policy["spec"]:
        policy["spec"]["ingress"] = rules
    return policy


def _selects_review_worker(selector: Mapping[str, Any] | None) -> bool:
    if selector is None:
        return False
    empty = not selector.get("matchLabels") and not selector.get("matchExpressions")
    return empty or _names(selector, REVIEW_WORKER)


def _review_policy(policy: dict[str, Any], tenant: Tenant) -> dict[str, Any]:
    """Copy a platform policy that selects the review worker into the tenant's review namespace."""
    policy = _worker_policy(policy, tenant)
    policy["metadata"]["namespace"] = tenant.review_namespace
    return policy


def _binds_worker(binding: Mapping[str, Any]) -> bool:
    return any(
        subject.get("kind") == "ServiceAccount"
        and subject.get("name") == WORKER
        and subject.get("namespace") == PLATFORM_NAMESPACE
        for subject in binding.get("subjects", [])
    )


def _review_sandbox_binding(binding: dict[str, Any], tenant: Tenant) -> dict[str, Any]:
    """Bind the tenant's review worker to the sandbox Role the activity worker holds."""
    binding = copy.deepcopy(binding)
    binding["metadata"]["name"] = binding["metadata"]["name"].replace(WORKER, REVIEW_WORKER)
    binding["metadata"]["namespace"] = tenant.sandbox_namespace
    binding["subjects"] = [
        {"kind": "ServiceAccount", "name": REVIEW_WORKER, "namespace": tenant.review_namespace}
    ]
    return binding


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


IMAGE_POLICY = "agentic-dev-platform-tenant-images"
IMAGE_POLICY_PARAMS = "image-policy"
_DOCKER_LIBRARY = "docker.io/library/"
_DOCKER_HUB = "docker.io/"

# The kubelet pulls a pod's image as written, so the policy matches the written form. A Docker Hub
# image may be written without its registry or library path, so each Docker Hub entry also admits
# its short form.
_IMAGE_RULE = (
    "variables.images.all(i, i.contains('@sha256:') && variables.allowed.exists(p, "
    "p.endsWith('/') ? i.startsWith(p) : (i.startsWith(p + ':') || i.startsWith(p + '@'))))"
)


def kubelet_prefixes(entries: Iterable[str]) -> list[str]:
    """Return each image policy entry with the short forms a pod spec may write it in."""
    prefixes = []
    for entry in entries:
        prefixes.append(entry)
        if entry.startswith(_DOCKER_LIBRARY):
            prefixes.append(entry.removeprefix(_DOCKER_LIBRARY))
        if entry.startswith(_DOCKER_HUB):
            prefixes.append(entry.removeprefix(_DOCKER_HUB))
    return [prefix for prefix in dict.fromkeys(prefixes) if prefix]


def image_policy() -> dict[str, Any]:
    """Return the admission policy that refuses a tenant pod image the image policy does not admit.

    The policy reads each tenant's admitted prefixes from the image-policy ConfigMap of the tenant's
    sandbox namespace, through that namespace's binding.
    """
    images = " + ".join(
        [
            "object.spec.containers.map(c, c.image)",
            "(has(object.spec.initContainers) ? object.spec.initContainers.map(c, c.image) : [])",
            "(has(object.spec.ephemeralContainers) ? "
            "object.spec.ephemeralContainers.map(c, c.image) : [])",
        ]
    )
    return {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": IMAGE_POLICY, "labels": {PART_OF_LABEL: PLATFORM_NAMESPACE}},
        "spec": {
            "failurePolicy": "Fail",
            "paramKind": {"apiVersion": "v1", "kind": "ConfigMap"},
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE", "UPDATE"],
                        "resources": ["pods", "pods/ephemeralcontainers"],
                    }
                ]
            },
            "variables": [
                {"name": "images", "expression": images},
                {
                    "name": "allowed",
                    "expression": "params.data.prefixes.split('\\n').filter(p, p != '')",
                },
            ],
            "validations": [
                {
                    "expression": _IMAGE_RULE,
                    "messageExpression": (
                        "'a tenant pod image needs an @sha256: digest and a repository the image "
                        "policy admits, and this pod runs ' + variables.images.join(', ')"
                    ),
                }
            ],
        },
    }


def _image_policy_objects(tenant: Tenant) -> list[dict[str, Any]]:
    params = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": IMAGE_POLICY_PARAMS, "namespace": tenant.sandbox_namespace},
        "data": {"prefixes": "\n".join(kubelet_prefixes(tenant.image_prefixes))},
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {
            "name": f"{tenant.sandbox_namespace}-images",
            "labels": {PART_OF_LABEL: PLATFORM_NAMESPACE, TENANT_LABEL: tenant.name},
        },
        "spec": {
            "policyName": IMAGE_POLICY,
            "validationActions": ["Deny"],
            "paramRef": {
                "name": IMAGE_POLICY_PARAMS,
                "namespace": tenant.sandbox_namespace,
                "parameterNotFoundAction": "Deny",
            },
            "matchResources": {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": tenant.sandbox_namespace}
                }
            },
        },
    }
    return [params, binding]


def _quota(tenant: Tenant) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "sandbox", "namespace": tenant.sandbox_namespace},
        "spec": {"hard": dict(tenant.quota)},
    }


def _tenant_review_objects(
    documents: list[dict[str, Any]],
    review: dict[str, Any],
    tenant: Tenant,
    services: list[str],
    copied: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Render the tenant's review namespace from the platform review worker.

    The namespace holds the review worker, its ServiceAccount, the config it reads, the platform
    policies that select it, and the tenant's review workspace. The binding in the tenant's sandbox
    namespace lets it start the tenant's gate and scanner pods there.
    """
    rendered = [
        _namespace(tenant.review_namespace, tenant.name, REVIEW_ROLE),
        _tenant_review_worker(review, tenant, services),
    ]
    for document in documents:
        kind, namespace, name = _key(document)
        if namespace == SANDBOX_NAMESPACE and kind == "RoleBinding" and _binds_worker(document):
            rendered.append(_review_sandbox_binding(document, tenant))
        if namespace != PLATFORM_NAMESPACE:
            continue
        selector = (document.get("spec") or {}).get("podSelector")
        if (kind, name) in copied:
            document = copy.deepcopy(document)
            document["metadata"]["namespace"] = tenant.review_namespace
            if kind == "ConfigMap" and name in tenant.config:
                document["data"] = dict(tenant.config[name])
            rendered.append(document)
        elif kind == "NetworkPolicy" and _selects_review_worker(selector):
            rendered.append(_review_policy(document, tenant))
        elif kind == "SecurityGroupPolicy" and _names(selector, REVIEW_WORKER):
            document = copy.deepcopy(document)
            document["metadata"]["namespace"] = tenant.review_namespace
            rendered.append(document)
    return rendered + [copy.deepcopy(document) for document in tenant.review_workspace]


def tenantize(documents: list[dict[str, Any]], tenants: Iterable[Tenant]) -> list[dict[str, Any]]:
    """Render each tenant's namespaces from the platform worker and the sandbox namespace."""
    tenants = list(tenants)
    deployment = _find(documents, "Deployment", PLATFORM_NAMESPACE, WORKER)
    if deployment is None or not tenants:
        return documents
    spec = _pod_spec(deployment)
    config = _referenced_config(spec)
    provider = _secret_provider(spec)
    gateway = _find(documents, "Deployment", PLATFORM_NAMESPACE, GATEWAY)
    gateway_spec = (_pod_spec(gateway) if gateway is not None else None) or {}
    gateway_config = _referenced_config(gateway_spec)
    gateway_provider = _secret_provider(gateway_spec)
    gateway_objects = (
        {("Service", GATEWAY), ("Ingress", GATEWAY), ("SecretProviderClass", gateway_provider)}
        if gateway is not None
        else set()
    )
    gateway_retired = (
        gateway_objects | {("Deployment", GATEWAY), ("ServiceAccount", GATEWAY)}
        if gateway is not None
        else set()
    )
    services = [name for kind, namespace, name in map(_key, documents) if kind == "Service"]
    review = _find(documents, "Deployment", PLATFORM_NAMESPACE, REVIEW_WORKER)
    review_spec = (_pod_spec(review) if review is not None else None) or {}
    review_config = _referenced_config(review_spec)
    review_objects = (
        {("ServiceAccount", REVIEW_WORKER), ("SecretProviderClass", _secret_provider(review_spec))}
        if review is not None
        else set()
    )
    review_retired = (
        review_objects | {("Deployment", REVIEW_WORKER)} if review is not None else set()
    )
    shared = set().union(
        *(
            _referenced_config(_pod_spec(d))
            for d in documents
            if _pod_spec(d) is not None
            and d["metadata"].get("namespace") == PLATFORM_NAMESPACE
            and d["metadata"]["name"] not in (WORKER, GATEWAY, REVIEW_WORKER)
        )
    )
    moved_config = (config | gateway_config | review_config) - shared
    claim = _find(documents, "PersistentVolumeClaim", PLATFORM_NAMESPACE, WORKSPACE_CLAIM)
    volume = (claim or {}).get("spec", {}).get("volumeName", "")

    def retired(document: dict[str, Any]) -> bool:
        kind, namespace, name = _key(document)
        if namespace == SANDBOX_NAMESPACE or (kind == "Namespace" and name == SANDBOX_NAMESPACE):
            return True
        if kind == "PersistentVolume" and volume and name == volume:
            return True
        if namespace == PLATFORM_NAMESPACE and (kind, name) in (
            moved_config | gateway_retired | review_retired
        ):
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
        worker = _tenant_worker(deployment, tenant, services)
        rendered.append(worker)
        if gateway is not None:
            rendered.append(_tenant_gateway(gateway, worker, tenant, config, services))
            for document in documents:
                kind, namespace, name = _key(document)
                if namespace != PLATFORM_NAMESPACE:
                    continue
                if (kind, name) in gateway_objects:
                    copied = copy.deepcopy(document)
                    copied["metadata"]["namespace"] = tenant.namespace
                    copied["metadata"].setdefault("labels", {})[TENANT_LABEL] = tenant.name
                    rendered.append(copied)
                elif kind == "NetworkPolicy" and _names(
                    document["spec"].get("podSelector"), GATEWAY
                ):
                    rendered.append(_gateway_policy(document, tenant, gateway))
                elif kind == "SecurityGroupPolicy" and _names(
                    document["spec"].get("podSelector"), GATEWAY
                ):
                    copied = copy.deepcopy(document)
                    copied["metadata"]["namespace"] = tenant.namespace
                    copied["spec"]["podSelector"] = {"matchLabels": {NAME_LABEL: GATEWAY}}
                    rendered.append(copied)
        for document in tenant.workspace:
            rendered.append(copy.deepcopy(document))
        if review is not None:
            rendered += _tenant_review_objects(
                documents, review, tenant, services, review_config | review_objects
            )
        if tenant.quota:
            rendered.append(_quota(tenant))
        if tenant.image_prefixes:
            rendered += _image_policy_objects(tenant)
    if any(tenant.image_prefixes for tenant in tenants):
        rendered.append(image_policy())
    return sorted(rendered, key=lambda document: document["kind"] != "Namespace")
