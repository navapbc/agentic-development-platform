"""Mount each tenant's webhook secrets in the EKS inbound webhook and point it at the bundles.

The eks-deployer image runs this module with the Python standard library and the scripts it
copies, so it imports nothing from the webhook service.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
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
# The tenant's worker signs its Slack actions with the tenant's own action key, so the webhook
# mounts the tenant's Slack secrets even when it serves one tenant.
SLACK_WEBHOOK_KEYS = frozenset({"slack_signing_secret", "escalation_action_key"})


def webhook_alias(prefix: str, tenant: str) -> str:
    return f"{prefix}{tenant.upper()}"


def webhook_deployment(documents: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return _platform_document(documents, "Deployment", WEBHOOK)


def _platform_document(
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
    returned. With one tenant, the webhook keeps the platform's GitHub and Jira secrets and its
    config, and mounts only the tenant's Slack signing secret and action key.
    """
    entries = list(inputs.get("tenants") or [])
    if not entries:
        return []
    single = len(entries) < 2
    provider = _platform_document(documents, "SecretProviderClass", WEBHOOK)
    webhook = webhook_deployment(documents)
    if provider is None or webhook is None:
        raise RenderError(f"the overlay has no {WEBHOOK} Deployment and SecretProviderClass")
    buckets = {entry["config_bucket"] for entry in entries if entry.get("config_bucket")}
    if not single and len(buckets) != 1:
        raise RenderError(f"the {WEBHOOK} reads every tenant's bundle from one config bucket")
    keys = {
        key: prefix
        for key, prefix in TENANT_WEBHOOK_KEYS.items()
        if not single or key in SLACK_WEBHOOK_KEYS
    }
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
    if single:
        return skipped
    container = next(
        c for c in webhook["spec"]["template"]["spec"]["containers"] if c["name"] == WEBHOOK
    )
    container.setdefault("env", []).append({"name": "TENANT_CONFIG_BUCKET", "value": buckets.pop()})
    return skipped
