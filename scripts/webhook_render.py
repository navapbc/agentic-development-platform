"""Give the platform's inbound webhook each tenant's webhook secrets and routing files, locally.

With more than one tenant, the webhook routes each delivery to its tenant, so it reads every
tenant's repositories.yaml and jira.yaml and verifies each tenant's deliveries with that tenant's
secrets. A tenant's secrets are GITHUB_WEBHOOK_SECRET and JIRA_WEBHOOK_SECRET in its settings file,
config/<env>/tenants/<tenant>.env, and the home tenant without them uses those of `.env`. The
webhook receives them as GITHUB_WEBHOOK_SECRET_<TENANT> and JIRA_WEBHOOK_SECRET_<TENANT> and no
other value of any tenant's settings file. Two tenants may not share a secret, because either
could then sign the other's deliveries.

With one tenant, the webhook keeps the platform's secrets and config and is left unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from services.orchestration.webhook.tenants import (
    GITHUB_SIGNING_ENV_PREFIX,
    JIRA_SIGNING_ENV_PREFIX,
    secret_env_name,
)

from scripts.k8s_render import (
    PLATFORM_NAMESPACE,
    RenderInputs,
    config_map,
    home_tenant,
    secret,
    tenant_bundle,
)

WEBHOOK = "inbound-webhook"
WEBHOOK_TENANT_ENV = "inbound-webhook-tenants"
WEBHOOK_CONFIG = "inbound-webhook-tenant-config"
WEBHOOK_TENANT_ENV_HASH = "agentic-dev-platform/webhook-tenants-hash"
INTEGRATION_CONFIG = "integration-config"
ROUTING_FILES = ("repositories.yaml", "jira.yaml")
LOCAL_DEMO_SIGNING_KEY = "local-demo-secret"
SECRET_SETTINGS = {
    "GITHUB_WEBHOOK_SECRET": GITHUB_SIGNING_ENV_PREFIX,
    "JIRA_WEBHOOK_SECRET": JIRA_SIGNING_ENV_PREFIX,
}


def webhook_secret_values(
    names: Sequence[str],
    home: str,
    own: Mapping[str, Mapping[str, str] | None],
    env: Mapping[str, str | None],
) -> dict[str, str]:
    """Return each tenant's webhook secrets under the names the webhook reads them by."""
    values: dict[str, str] = {}
    for setting, prefix in SECRET_SETTINGS.items():
        holders: dict[str, str] = {}
        for tenant in names:
            value = ((own.get(tenant) or {}).get(setting) or "").strip()
            if not value and tenant == home:
                value = (env.get(setting) or "").strip() or LOCAL_DEMO_SIGNING_KEY
            if not value:
                continue
            if value in holders:
                raise ValueError(
                    f"tenants {holders[value]} and {tenant} share a {setting}. Set a {setting} "
                    f"of its own in config/<env>/tenants/{tenant}.env"
                )
            holders[value] = tenant
            values[secret_env_name(prefix, tenant)] = value
    return values


def webhook_routing_files(tenants_dir: Path, names: Iterable[str]) -> dict[str, str]:
    """Return each tenant bundle's routing files, keyed <tenant>.<file>."""
    files: dict[str, str] = {}
    for tenant in names:
        bundle = tenant_bundle(tenants_dir / tenant)
        for name in ROUTING_FILES:
            if name in bundle:
                files[f"{tenant}.{name}"] = bundle[name]
    return files


def _webhook(documents: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            document
            for document in documents
            if document["kind"] == "Deployment"
            and document["metadata"]["name"] == WEBHOOK
            and document["metadata"].get("namespace") == PLATFORM_NAMESPACE
        ),
        None,
    )


def _project_routing_files(pod: dict[str, Any], files: Iterable[str]) -> None:
    items = []
    for key in sorted(files):
        tenant, name = key.split(".", 1)
        items.append({"key": key, "path": f"tenants/{tenant}/{name}"})
    for volume in pod["volumes"]:
        if volume["name"] != INTEGRATION_CONFIG:
            continue
        source = volume.pop("configMap")
        volume["projected"] = {
            "sources": [
                {"configMap": source},
                {"configMap": {"name": WEBHOOK_CONFIG, "items": items}},
            ]
        }


def localize_webhook(
    documents: list[dict[str, Any]], inputs: RenderInputs, names: Sequence[str]
) -> list[dict[str, Any]]:
    """Add the webhook's tenant secrets and routing files when it serves more than one tenant."""
    from scripts.tenant_config import read_tenant_settings

    webhook = _webhook(documents)
    if webhook is None or len(names) < 2:
        return documents
    tenants_dir = inputs.root / "config" / inputs.settings.environment / "tenants"
    own = {tenant: read_tenant_settings(tenants_dir, tenant) for tenant in names}
    home = home_tenant(inputs.settings, names)
    secrets = webhook_secret_values(names, home, own, inputs.env)
    files = webhook_routing_files(tenants_dir, names)
    template = webhook["spec"]["template"]
    pod = template["spec"]
    container = next(c for c in pod["containers"] if c["name"] == WEBHOOK)
    container.setdefault("env", []).append({"name": "TENANTS", "value": ",".join(names)})
    container.setdefault("envFrom", []).append({"secretRef": {"name": WEBHOOK_TENANT_ENV}})
    if files:
        _project_routing_files(pod, files)
    digest = hashlib.sha256(json.dumps(secrets, sort_keys=True).encode()).hexdigest()[:16]
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    annotations[WEBHOOK_TENANT_ENV_HASH] = digest
    return [*documents, secret(WEBHOOK_TENANT_ENV, secrets), config_map(WEBHOOK_CONFIG, files)]


TENANT_WEBHOOK_KEYS = {
    "github_webhook_secret": GITHUB_SIGNING_ENV_PREFIX,
    "jira_webhook_secret": JIRA_SIGNING_ENV_PREFIX,
}


def _provider(documents: Iterable[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next(
        (
            document
            for document in documents
            if document.get("kind") == "SecretProviderClass"
            and document["metadata"]["name"] == name
            and document["metadata"].get("namespace") == PLATFORM_NAMESPACE
        ),
        None,
    )


def route_webhook_tenants(
    documents: list[dict[str, Any]], inputs: Mapping[str, Any], omit: Iterable[str]
) -> list[str]:
    """Mount each tenant's webhook secrets in the EKS webhook and point it at the tenant bundles.

    A tenant's secrets are the github_webhook_secret and jira_webhook_secret ARNs of its
    `secrets` input, mounted as GITHUB_WEBHOOK_SECRET_<TENANT> and JIRA_WEBHOOK_SECRET_<TENANT>.
    A secret omitted as `<tenant>:<key>` has no current value and is left out, and its alias is
    returned. With one tenant, the webhook keeps the platform's secrets and is left unchanged.
    """
    from scripts.eks_render import RenderError

    entries = list(inputs.get("tenants") or [])
    if len(entries) < 2:
        return []
    provider = _provider(documents, WEBHOOK)
    webhook = _webhook(documents)
    if provider is None or webhook is None:
        raise RenderError(f"the overlay has no {WEBHOOK} Deployment and SecretProviderClass")
    buckets = {entry["config_bucket"] for entry in entries if entry.get("config_bucket")}
    if len(buckets) != 1:
        raise RenderError(f"the {WEBHOOK} reads every tenant's bundle from one config bucket")
    omitted = set(omit)
    objects = json.loads(provider["spec"]["parameters"]["objects"])
    skipped: list[str] = []
    for entry in entries:
        tenant, own = entry["name"], entry.get("secrets") or {}
        for key, prefix in TENANT_WEBHOOK_KEYS.items():
            alias = secret_env_name(prefix, tenant)
            if key not in own:
                continue
            if f"{tenant}:{key}" in omitted:
                skipped.append(f"{WEBHOOK}:{alias}")
                continue
            objects.append(
                {"objectName": own[key], "objectType": "secretsmanager", "objectAlias": alias}
            )
    provider["spec"]["parameters"]["objects"] = json.dumps(objects)
    container = next(
        c for c in webhook["spec"]["template"]["spec"]["containers"] if c["name"] == WEBHOOK
    )
    container.setdefault("env", []).append({"name": "TENANT_CONFIG_BUCKET", "value": buckets.pop()})
    return skipped
