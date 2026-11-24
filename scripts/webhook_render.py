"""Give the platform's inbound webhook each tenant's webhook secrets and routing files, locally.

The webhook routes each delivery to its tenant, so it reads every tenant's repositories.yaml,
jira.yaml, and escalation.yaml and verifies each tenant's deliveries with that tenant's secrets. A
tenant's secrets are the GITHUB_WEBHOOK_SECRET, JIRA_WEBHOOK_SECRET, SLACK_SIGNING_SECRET, and
ESCALATION_ACTION_KEY of its settings file, config/local/tenants/<tenant>.env, and no other file
supplies them. The webhook receives them as GITHUB_WEBHOOK_SECRET_<TENANT>,
JIRA_WEBHOOK_SECRET_<TENANT>, SLACK_SIGNING_SECRET_<TENANT>, and ESCALATION_ACTION_KEY_<TENANT>,
and no other value of any tenant's settings file. Two tenants may not share a secret, because
either could then sign the other's deliveries.

Every webhook receives TENANTS, the tenants the local cluster runs. With one tenant, the webhook
also serves that tenant on the routes without a tenant segment, such as /webhook/jira.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from services.integration.credentials import env_config_dir
from services.integration.tenant_secrets import (
    ESCALATION_ACTION_KEY_ENV_PREFIX,
    ESCALATION_ACTION_KEY_SHA256_ENV_PREFIX,
    SLACK_SIGNING_ENV_PREFIX,
    action_key_fingerprint,
    secret_env_name,
)
from services.orchestration.webhook.tenants import (
    GITHUB_SIGNING_ENV_PREFIX,
    JIRA_SIGNING_ENV_PREFIX,
)

from scripts.k8s_render import (
    RenderInputs,
    config_map,
    secret,
    tenant_bundle,
)
from scripts.webhook_eks_render import (
    SHARED_APP_FILE,
    SHARED_SIGNING_SECRET_ALIAS,
    SLACK_POSTER,
    SLACK_POSTER_CA,
    SLACK_POSTER_CA_KEY,
    SOCKET_MODE_ALIAS,
    WEBHOOK,
    environment_runs_slack_poster,
    platform_document,
    webhook_deployment,
    without_slack_poster,
)

WEBHOOK_TENANT_ENV = "inbound-webhook-tenants"
WEBHOOK_CONFIG = "inbound-webhook-tenant-config"
WEBHOOK_TENANT_ENV_HASH = "agentic-dev-platform/webhook-tenants-hash"
INTEGRATION_CONFIG = "integration-config"
ROUTING_FILES = (
    "repositories.yaml",
    "jira.yaml",
    "escalation.yaml",
    "gates.yaml",
    "integrations.yaml",
)
# The integration-config ConfigMap lists fixed platform files, and kustomize fails on a listed file
# that is absent, so an optional platform routing file is projected here.
ENVIRONMENT_ROUTING_FILES = ("gates.yaml", SHARED_APP_FILE)
SHARED_BOT_TOKEN = "SLACK_SHARED_BOT_TOKEN"
SHARED_BOT_TOKEN_KEY = "slack_shared_bot_token"
SLACK_POSTER_BOT_HASH = "agentic-dev-platform/slack-poster-token-hash"
SLACK_POSTER_TLS = "slack-poster-tls"
SLACK_POSTER_TLS_FINGERPRINT = "agentic-dev-platform/slack-poster-tls-fingerprint"
SECRET_SETTINGS = {
    "GITHUB_WEBHOOK_SECRET": GITHUB_SIGNING_ENV_PREFIX,
    "JIRA_WEBHOOK_SECRET": JIRA_SIGNING_ENV_PREFIX,
    "SLACK_SIGNING_SECRET": SLACK_SIGNING_ENV_PREFIX,
    "ESCALATION_ACTION_KEY": ESCALATION_ACTION_KEY_ENV_PREFIX,
}


def webhook_secret_values(
    names: Sequence[str], own: Mapping[str, Mapping[str, str] | None]
) -> dict[str, str]:
    """Return each tenant's webhook secrets, read from its own settings, by their webhook names."""
    values: dict[str, str] = {}
    for setting, prefix in SECRET_SETTINGS.items():
        holders: dict[str, str] = {}
        for tenant in names:
            value = ((own.get(tenant) or {}).get(setting) or "").strip()
            if not value:
                continue
            if value in holders:
                raise ValueError(
                    f"tenants {holders[value]} and {tenant} share a {setting}. Set a {setting} "
                    f"of its own in config/local/tenants/{tenant}.env"
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


def environment_routing_files(environment_dir: Path) -> dict[str, str]:
    """Return the optional routing files the platform directory holds, keyed by file name."""
    return {
        name: (environment_dir / name).read_text(encoding="utf-8")
        for name in ENVIRONMENT_ROUTING_FILES
        if (environment_dir / name).is_file()
    }


def _project_routing_files(pod: dict[str, Any], files: Iterable[str]) -> None:
    items = []
    for key in sorted(files):
        if key in ENVIRONMENT_ROUTING_FILES:
            items.append({"key": key, "path": key})
            continue
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


def slack_poster_tls_objects(inputs: RenderInputs) -> list[dict[str, Any]]:
    """Return the posting service's CA ConfigMap and certificate Secret when the environment runs
    the service, issued from the local CA that scripts.slack_poster_tls keeps.

    Every worker mounts the CA ConfigMap, so these are part of the generated config that the
    tenant render copies into each tenant namespace.
    """
    from services.server_tls import LOOPBACK

    from scripts.slack_poster_tls import local_material

    environment_dir = env_config_dir(root=inputs.root)
    if not environment_runs_slack_poster(environment_dir):
        return []
    host = f"{SLACK_POSTER}.{inputs.settings.k8s_namespace}.svc.cluster.local"
    material = local_material([host, LOOPBACK])
    return [
        config_map(SLACK_POSTER_CA, {SLACK_POSTER_CA_KEY: material.ca}),
        secret(SLACK_POSTER_TLS, {"tls.crt": material.certificate, "tls.key": material.key}),
    ]


def localize_webhook(
    documents: list[dict[str, Any]], inputs: RenderInputs, names: Sequence[str]
) -> list[dict[str, Any]]:
    """Name the webhook's tenants, and add the platform's optional routing files and each
    tenant's secrets and routing files.

    The webhook always receives TENANTS, so a single-tenant webhook serves the tenant whose
    worker polls its task queue.
    """
    from scripts.tenant_config import read_tenant_settings

    webhook = webhook_deployment(documents)
    if webhook is None:
        return documents
    template = webhook["spec"]["template"]
    pod = template["spec"]
    container = next(c for c in pod["containers"] if c["name"] == WEBHOOK)
    container.setdefault("env", []).append({"name": "TENANTS", "value": ",".join(names)})
    environment_dir = env_config_dir(root=inputs.root)
    files = environment_routing_files(environment_dir)
    runs_poster = environment_runs_slack_poster(environment_dir)
    tenants_dir = environment_dir / "tenants"
    files.update(webhook_routing_files(tenants_dir, names))
    own = {tenant: read_tenant_settings(tenants_dir, tenant) for tenant in names}
    secrets = webhook_secret_values(names, own)
    for alias in (SHARED_SIGNING_SECRET_ALIAS, SOCKET_MODE_ALIAS):
        value = (inputs.env.get(alias) or "").strip()
        if runs_poster and value:
            secrets[alias] = value
    action_keys = {
        tenant: secrets[name]
        for tenant in names
        if (name := secret_env_name(ESCALATION_ACTION_KEY_ENV_PREFIX, tenant)) in secrets
    }
    documents = (
        localize_slack_poster(documents, inputs, names, files, action_keys)
        if runs_poster
        else without_slack_poster(documents)
    )
    added = []
    if secrets:
        container.setdefault("envFrom", []).append({"secretRef": {"name": WEBHOOK_TENANT_ENV}})
        digest = hashlib.sha256(json.dumps(secrets, sort_keys=True).encode()).hexdigest()[:16]
        annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
        annotations[WEBHOOK_TENANT_ENV_HASH] = digest
        added.append(secret(WEBHOOK_TENANT_ENV, secrets))
    if files:
        _project_routing_files(pod, files)
        added.append(config_map(WEBHOOK_CONFIG, files))
    return [*documents, *added]


def localize_slack_poster(
    documents: list[dict[str, Any]],
    inputs: RenderInputs,
    names: Sequence[str],
    files: Mapping[str, str],
    action_keys: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Point the Slack posting service at the routing files the webhook reads.

    The service holds the shared app's bot token in a Secret no other pod mounts, and receives
    each tenant's action key fingerprint, never the key.
    """
    poster = platform_document(documents, "Deployment", SLACK_POSTER)
    if poster is None:
        return documents
    template = poster["spec"]["template"]
    pod = template["spec"]
    container = next(c for c in pod["containers"] if c["name"] == SLACK_POSTER)
    container.setdefault("env", []).append({"name": "TENANTS", "value": ",".join(names)})
    container["env"].extend(
        {
            "name": secret_env_name(ESCALATION_ACTION_KEY_SHA256_ENV_PREFIX, tenant),
            "value": action_key_fingerprint(key),
        }
        for tenant, key in sorted(action_keys.items())
    )
    _project_routing_files(pod, files)
    token = (inputs.env.get(SHARED_BOT_TOKEN) or "").strip()
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    annotations[SLACK_POSTER_BOT_HASH] = hashlib.sha256(token.encode()).hexdigest()[:16]
    tls = platform_document(documents, "Secret", SLACK_POSTER_TLS)
    if tls is not None:
        leaf = base64.b64decode(tls["data"]["tls.crt"])
        annotations[SLACK_POSTER_TLS_FINGERPRINT] = hashlib.sha256(leaf).hexdigest()[:16]
    return [*documents, secret(SLACK_POSTER, {SHARED_BOT_TOKEN_KEY: token})]
