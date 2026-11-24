"""Mount each tenant's webhook secrets in the EKS inbound webhook and point it at the bundles.

The eks-deployer image runs this module with the Python standard library and the scripts it
copies, so it imports nothing from the webhook service.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts.eks_render import PLATFORM_NAMESPACE, RenderError

WEBHOOK = "inbound-webhook"
TENANT_WEBHOOK_KEYS = {
    key: f"{key.upper()}_"
    for key in (
        "github_webhook_secret",
        "jira_webhook_secret",
        "slack_signing_secret",
        "escalation_action_key",
    )
}
ACTION_KEY_FINGERPRINT_PREFIX = "ESCALATION_ACTION_KEY_SHA256_"


def webhook_alias(prefix: str, tenant: str) -> str:
    return f"{prefix}{tenant.upper()}"


def webhook_deployment(documents: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return platform_document(documents, "Deployment", WEBHOOK)


def platform_document(
    documents: Iterable[dict[str, Any]], kind: str, name: str
) -> dict[str, Any] | None:
    return next(
        (
            document
            for document in documents
            if document.get("kind") == kind
            and document["metadata"]["name"] == name
            and document["metadata"].get("namespace") == PLATFORM_NAMESPACE
        ),
        None,
    )


def route_webhook_tenants(
    documents: list[dict[str, Any]], inputs: Mapping[str, Any], omit: Iterable[str]
) -> list[str]:
    """Mount each tenant's webhook secrets in the EKS webhook and point it at the tenant bundles.

    A tenant's secrets are the github_webhook_secret, jira_webhook_secret, slack_signing_secret,
    and escalation_action_key ARNs of its `secrets` input, mounted as
    GITHUB_WEBHOOK_SECRET_<TENANT>, JIRA_WEBHOOK_SECRET_<TENANT>, SLACK_SIGNING_SECRET_<TENANT>,
    and ESCALATION_ACTION_KEY_<TENANT>.
    A secret omitted as `<tenant>:<key>` has no current value and is left out, and its alias is
    returned. One tenant is served the same way as several.
    """
    entries = list(inputs.get("tenants") or [])
    if not entries:
        return []
    provider = platform_document(documents, "SecretProviderClass", WEBHOOK)
    webhook = webhook_deployment(documents)
    if provider is None or webhook is None:
        raise RenderError(f"the overlay has no {WEBHOOK} Deployment and SecretProviderClass")
    buckets = {entry["config_bucket"] for entry in entries if entry.get("config_bucket")}
    if len(buckets) != 1:
        raise RenderError(f"the {WEBHOOK} reads every tenant's bundle from one config bucket")
    keys = TENANT_WEBHOOK_KEYS
    omitted = set(omit)
    objects = json.loads(provider["spec"]["parameters"]["objects"])
    skipped: list[str] = []
    for entry in entries:
        tenant, own = entry["name"], entry.get("secrets") or {}
        for key, prefix in keys.items():
            alias = webhook_alias(prefix, tenant)
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


def route_action_key_fingerprints(
    documents: list[dict[str, Any]], inputs: Mapping[str, Any]
) -> None:
    """Mount each tenant's action key fingerprint in the Slack posting service.

    The service compares the fingerprints to refuse the tenants that share an action key, as the
    webhook does with the keys, and holds no key itself.
    """
    if not runs_slack_poster(inputs):
        return
    provider = platform_document(documents, "SecretProviderClass", SLACK_POSTER)
    if provider is None:
        raise RenderError(f"the overlay has no {SLACK_POSTER} SecretProviderClass")
    objects = json.loads(provider["spec"]["parameters"]["objects"])
    objects.extend(
        {
            "objectName": entry["action_key_fingerprint"],
            "objectType": "secretsmanager",
            "objectAlias": webhook_alias(ACTION_KEY_FINGERPRINT_PREFIX, entry["name"]),
        }
        for entry in inputs.get("tenants") or []
        if entry.get("action_key_fingerprint")
    )
    provider["spec"]["parameters"]["objects"] = json.dumps(objects)


SLACK_POSTER = "slack-poster"
SHARED_APP_FILE = "slack-shared-app.yaml"
SLACK_POSTER_CA = "slack-poster-ca"
SLACK_POSTER_CA_KEY = "ca.pem"
SHARED_SIGNING_SECRET_ALIAS = "SLACK_SHARED_SIGNING_SECRET"
SOCKET_MODE_ALIAS = "SLACK_SHARED_APP_TOKEN"
SHARED_WEBHOOK_ALIASES = frozenset({SHARED_SIGNING_SECRET_ALIAS, SOCKET_MODE_ALIAS})
SLACK_POSTER_OBJECTS = frozenset(
    {
        ("ServiceAccount", SLACK_POSTER),
        ("Deployment", SLACK_POSTER),
        ("Service", SLACK_POSTER),
        ("SecretProviderClass", SLACK_POSTER),
        ("NetworkPolicy", f"{SLACK_POSTER}-ingress"),
        ("ClusterRole", f"agentic-dev-platform-{SLACK_POSTER}-tokenreview"),
        ("ClusterRoleBinding", f"agentic-dev-platform-{SLACK_POSTER}-tokenreview"),
    }
)


def without_slack_poster(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        document
        for document in documents
        if (document.get("kind"), document.get("metadata", {}).get("name"))
        not in SLACK_POSTER_OBJECTS
    ]


def runs_slack_poster(inputs: Mapping[str, Any]) -> bool:
    return SHARED_APP_FILE in (inputs.get("environment_config") or {})


def environment_runs_slack_poster(environment_dir: Path) -> bool:
    return (environment_dir / SHARED_APP_FILE).is_file()


def slack_poster_ca(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the slack-poster-ca ConfigMap the workers verify the posting service with.

    It carries every certificate of PLATFORM_CA_PEM, the CA that issues the posting service's
    certificate, so a CA rotation can hold the old and the new CA at once.
    """
    from scripts.eks_render import config_map

    if not runs_slack_poster(inputs):
        return []
    ca = ((inputs.get("platform_environment") or {}).get("PLATFORM_CA_PEM") or "").strip()
    if not ca:
        raise RenderError(
            f"the {SLACK_POSTER} serves a certificate of the platform CA, and PLATFORM_CA_PEM is "
            "empty. Set container_platform_ca_file to the CA that inv slack-poster-certs issues "
            "from"
        )
    return [config_map(SLACK_POSTER_CA, {SLACK_POSTER_CA_KEY: ca + "\n"})]


def route_slack_poster(
    documents: list[dict[str, Any]], inputs: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Run the Slack posting service only for an environment that defines a shared Slack app.

    Without the shared app, the posting service and the webhook's shared signing secret and app
    token are left out. With it, the posting service reads the bundles of the tenants the inputs
    name.
    """
    if not runs_slack_poster(inputs):
        kept = without_slack_poster(documents)
        provider = platform_document(kept, "SecretProviderClass", WEBHOOK)
        if provider is not None:
            objects = json.loads(provider["spec"]["parameters"]["objects"])
            provider["spec"]["parameters"]["objects"] = json.dumps(
                [
                    entry
                    for entry in objects
                    if entry.get("objectAlias") not in SHARED_WEBHOOK_ALIASES
                ]
            )
        return kept
    poster = platform_document(documents, "Deployment", SLACK_POSTER)
    if poster is None:
        raise RenderError(f"the overlay has no {SLACK_POSTER} Deployment")
    entries = list(inputs.get("tenants") or [])
    buckets = {entry["config_bucket"] for entry in entries if entry.get("config_bucket")}
    if len(buckets) != 1:
        raise RenderError(f"the {SLACK_POSTER} reads every tenant's bundle from one config bucket")
    container = next(
        c for c in poster["spec"]["template"]["spec"]["containers"] if c["name"] == SLACK_POSTER
    )
    container.setdefault("env", []).extend(
        [
            {"name": "TENANTS", "value": ",".join(entry["name"] for entry in entries)},
            {"name": "TENANT_CONFIG_BUCKET", "value": buckets.pop()},
        ]
    )
    return documents
