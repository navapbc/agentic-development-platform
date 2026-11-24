"""The EKS webhook mounts each tenant's webhook secrets and reads every tenant's bundle."""

from __future__ import annotations

import json
import shutil
from typing import Any

import pytest
import yaml
from scripts import eks_render, webhook_eks_render
from services import process
from tests.unit.test_eks_render import OVERLAY, REPOSITORY, _inputs, _named, _tenant

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl renders it")

ARN = "arn:aws:secretsmanager:us-west-2:9:secret:tenants"


@pytest.fixture(scope="module")
def overlay() -> list[dict[str, Any]]:
    rendered = process.run(
        ["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", str(OVERLAY)],
        timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _with_webhook_secrets(name: str) -> dict[str, Any]:
    tenant = _tenant(name)
    for key in webhook_eks_render.TENANT_WEBHOOK_KEYS:
        tenant["secrets"][key] = f"{ARN}/{name}/{key}"
    return tenant


def _webhook_objects(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provider = _named(documents, "SecretProviderClass", "inbound-webhook", "agentic-dev-platform")
    return json.loads(provider["spec"]["parameters"]["objects"])


def _webhook_env(documents: list[dict[str, Any]]) -> dict[str, str]:
    webhook = _named(documents, "Deployment", "inbound-webhook", "agentic-dev-platform")
    container = webhook["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry.get("value", "") for entry in container["env"]}


def test_the_webhook_mounts_each_tenants_webhook_secrets_and_no_other(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp"), _with_webhook_secrets("beta")]

    documents, skipped = eks_render.render(overlay, inputs, REPOSITORY)

    tenant_objects = {
        entry["objectAlias"]: entry["objectName"]
        for entry in _webhook_objects(documents)
        if entry["objectName"].startswith(ARN)
    }
    assert tenant_objects == {
        "GITHUB_WEBHOOK_SECRET_ADP": f"{ARN}/adp/github_webhook_secret",
        "JIRA_WEBHOOK_SECRET_ADP": f"{ARN}/adp/jira_webhook_secret",
        "SLACK_SIGNING_SECRET_ADP": f"{ARN}/adp/slack_signing_secret",
        "ESCALATION_ACTION_KEY_ADP": f"{ARN}/adp/escalation_action_key",
        "GITHUB_WEBHOOK_SECRET_BETA": f"{ARN}/beta/github_webhook_secret",
        "JIRA_WEBHOOK_SECRET_BETA": f"{ARN}/beta/jira_webhook_secret",
        "SLACK_SIGNING_SECRET_BETA": f"{ARN}/beta/slack_signing_secret",
        "ESCALATION_ACTION_KEY_BETA": f"{ARN}/beta/escalation_action_key",
    }
    assert _webhook_env(documents)["TENANT_CONFIG_BUCKET"] == "agentic-platform-tenant-config-abc"
    assert skipped == []
    worker = _named(documents, "SecretProviderClass", "activity-worker", "tenant-beta")
    worker_objects = {
        entry["objectAlias"]: entry["objectName"]
        for entry in json.loads(worker["spec"]["parameters"]["objects"])
    }
    assert not [name for name in worker_objects.values() if "webhook" in name or "slack" in name]
    assert worker_objects["ESCALATION_ACTION_KEY"] == f"{ARN}/beta/escalation_action_key"


def test_a_tenant_webhook_secret_without_a_value_is_left_out(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp"), _with_webhook_secrets("beta")]

    documents, skipped = eks_render.render(
        overlay, inputs, REPOSITORY, omit_secrets=["beta:jira_webhook_secret"]
    )

    aliases = {entry["objectAlias"] for entry in _webhook_objects(documents)}
    assert "JIRA_WEBHOOK_SECRET_BETA" not in aliases
    assert "GITHUB_WEBHOOK_SECRET_BETA" in aliases
    assert "inbound-webhook:JIRA_WEBHOOK_SECRET_BETA" in skipped


def test_a_single_tenant_webhook_mounts_its_own_secrets_and_reads_its_bundle(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp")]

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    assert {entry["objectAlias"] for entry in _webhook_objects(documents)} == {
        "database-platform-app",
        "TEMPORAL_TLS_CA",
        "GITHUB_WEBHOOK_SECRET_ADP",
        "JIRA_WEBHOOK_SECRET_ADP",
        "SLACK_SIGNING_SECRET_ADP",
        "ESCALATION_ACTION_KEY_ADP",
    }
    assert _webhook_env(documents)["TENANT_CONFIG_BUCKET"]


def test_a_single_tenant_webhook_reads_the_bucket_the_slack_poster_reads(overlay) -> None:
    inputs = _with_shared_app(_inputs())
    inputs["tenants"] = [_with_webhook_secrets("adp")]

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    poster = _named(documents, "Deployment", "slack-poster", "agentic-dev-platform")
    poster_env = {
        e["name"]: e.get("value", "")
        for e in poster["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert _webhook_env(documents)["TENANT_CONFIG_BUCKET"] == poster_env["TENANT_CONFIG_BUCKET"]
    assert poster_env["TENANT_CONFIG_BUCKET"]


def test_tenants_in_different_config_buckets_are_refused(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp"), _with_webhook_secrets("beta")]
    inputs["tenants"][1]["config_bucket"] = "another-bucket"

    with pytest.raises(eks_render.RenderError, match="one config bucket"):
        eks_render.render(overlay, inputs, REPOSITORY)


def test_the_eks_webhook_aliases_match_the_names_the_webhook_reads() -> None:
    from services.integration.tenant_secrets import (
        ESCALATION_ACTION_KEY_ENV_PREFIX,
        ESCALATION_ACTION_KEY_SHA256_ENV_PREFIX,
        SLACK_SIGNING_ENV_PREFIX,
        secret_env_name,
    )
    from services.orchestration.webhook.tenants import (
        GITHUB_SIGNING_ENV_PREFIX,
        JIRA_SIGNING_ENV_PREFIX,
    )

    assert webhook_eks_render.TENANT_WEBHOOK_KEYS == {
        "github_webhook_secret": GITHUB_SIGNING_ENV_PREFIX,
        "jira_webhook_secret": JIRA_SIGNING_ENV_PREFIX,
        "slack_signing_secret": SLACK_SIGNING_ENV_PREFIX,
        "escalation_action_key": ESCALATION_ACTION_KEY_ENV_PREFIX,
    }
    assert webhook_eks_render.webhook_alias("x", "beta") == secret_env_name("x", "beta")
    assert (
        webhook_eks_render.ACTION_KEY_FINGERPRINT_PREFIX == ESCALATION_ACTION_KEY_SHA256_ENV_PREFIX
    )


def _poster_objects(documents: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (document["kind"], document["metadata"]["name"])
        for document in documents
        if (document["kind"], document["metadata"]["name"])
        in webhook_eks_render.SLACK_POSTER_OBJECTS
    ]


PLATFORM_CA = "-----BEGIN CERTIFICATE-----\nplatform-ca\n-----END CERTIFICATE-----"


def _with_shared_app(inputs: dict[str, Any]) -> dict[str, Any]:
    inputs["environment_config"] = {
        **(inputs.get("environment_config") or {}),
        "slack-shared-app.yaml": "config_version: 1\n",
    }
    inputs["platform_environment"] = {
        **(inputs.get("platform_environment") or {}),
        "PLATFORM_CA_PEM": PLATFORM_CA,
    }
    for key in (
        "slack_shared_bot_token",
        "slack_shared_signing_secret",
        "slack_shared_app_token",
        "slack_poster_tls",
    ):
        inputs["secrets"][key] = f"arn:aws:secretsmanager:us-west-2:9:secret:platform/{key}"
    for tenant in inputs["tenants"]:
        tenant["config_bucket"] = "agentic-platform-tenant-config"
    return inputs


def test_a_shared_app_with_no_tenant_is_refused_naming_the_tenants(overlay) -> None:
    inputs = _with_shared_app(_inputs())
    inputs["tenants"] = []

    with pytest.raises(eks_render.RenderError, match="the inputs name no tenant"):
        eks_render.render(overlay, inputs, REPOSITORY)


def test_every_worker_namespace_trusts_the_platform_ca_for_the_slack_poster(overlay) -> None:
    inputs = _with_shared_app(_inputs())
    inputs["tenants"] = [_tenant("adp"), _tenant("beta")]
    for tenant in inputs["tenants"]:
        tenant["config_bucket"] = "agentic-platform-tenant-config"

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    for namespace in ("agentic-dev-platform", "tenant-adp", "tenant-beta"):
        ca = _named(documents, "ConfigMap", "slack-poster-ca", namespace)
        assert ca["data"] == {"ca.pem": PLATFORM_CA + "\n"}
    worker = _named(documents, "Deployment", "activity-worker", "tenant-adp")
    container = worker["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert env["SLACK_POSTING_CA_FILE"] == "/etc/slack-poster-ca/ca.pem"
    assert {"name": "slack-poster-ca", "mountPath": "/etc/slack-poster-ca", "readOnly": True} in (
        container["volumeMounts"]
    )


def test_a_slack_poster_without_the_platform_ca_is_refused(overlay) -> None:
    inputs = _with_shared_app(_inputs())
    inputs["platform_environment"].pop("PLATFORM_CA_PEM")

    with pytest.raises(eks_render.RenderError, match="PLATFORM_CA_PEM is empty"):
        eks_render.render(overlay, inputs, REPOSITORY)


def test_without_a_shared_app_the_environment_runs_no_slack_poster(overlay) -> None:
    documents, _ = eks_render.render(overlay, _inputs(), REPOSITORY)

    assert _poster_objects(documents) == []
    aliases = {entry["objectAlias"] for entry in _webhook_objects(documents)}
    assert "SLACK_SHARED_SIGNING_SECRET" not in aliases


@pytest.mark.parametrize("shared", [True, False], ids=["shared-app", "no-shared-app"])
def test_the_shared_apps_app_token_mounts_only_into_the_webhook_with_a_shared_app(
    overlay, shared: bool
) -> None:
    inputs = _with_shared_app(_inputs()) if shared else _inputs()

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    webhook_aliases = {entry["objectAlias"] for entry in _webhook_objects(documents)}
    assert ("SLACK_SHARED_APP_TOKEN" in webhook_aliases) is shared
    if shared:
        poster = _named(documents, "SecretProviderClass", "slack-poster", "agentic-dev-platform")
        assert "SLACK_SHARED_APP_TOKEN" not in poster["spec"]["parameters"]["objects"]


def test_the_shared_apps_bot_token_mounts_only_into_the_slack_poster(overlay) -> None:
    inputs = _with_shared_app(_inputs())
    inputs["tenants"] = [_tenant("adp"), _tenant("beta")]
    for tenant in inputs["tenants"]:
        tenant["config_bucket"] = "agentic-platform-tenant-config"

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    provider = _named(documents, "SecretProviderClass", "slack-poster", "agentic-dev-platform")
    poster_aliases = [
        entry["objectAlias"] for entry in json.loads(provider["spec"]["parameters"]["objects"])
    ]
    assert poster_aliases == ["SLACK_SHARED_BOT_TOKEN", "slack-poster-tls"]
    webhook_aliases = {entry["objectAlias"] for entry in _webhook_objects(documents)}
    assert "SLACK_SHARED_SIGNING_SECRET" in webhook_aliases
    assert "SLACK_SHARED_BOT_TOKEN" not in webhook_aliases
    poster = _named(documents, "Deployment", "slack-poster", "agentic-dev-platform")
    env = {
        e["name"]: e.get("value", "")
        for e in poster["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["TENANTS"] == "adp,beta"
    assert env["TENANT_CONFIG_BUCKET"] == "agentic-platform-tenant-config"
    assert poster["spec"]["replicas"] == 1


def test_the_slack_poster_mounts_each_action_key_fingerprint_and_no_key(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp"), _with_webhook_secrets("beta")]
    _with_shared_app(inputs)
    for entry in inputs["tenants"]:
        entry["action_key_fingerprint"] = f"arn:fingerprint:{entry['name']}"

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    provider = _named(documents, "SecretProviderClass", "slack-poster", "agentic-dev-platform")
    objects = json.loads(provider["spec"]["parameters"]["objects"])
    assert {
        entry["objectAlias"]: entry["objectName"]
        for entry in objects
        if "ACTION_KEY" in entry["objectAlias"]
    } == {
        "ESCALATION_ACTION_KEY_SHA256_ADP": "arn:fingerprint:adp",
        "ESCALATION_ACTION_KEY_SHA256_BETA": "arn:fingerprint:beta",
    }


def test_the_slack_poster_admits_only_activity_workers_in_worker_namespaces(overlay) -> None:
    inputs = _with_shared_app(_inputs())

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    policy = _named(documents, "NetworkPolicy", "slack-poster-ingress", "agentic-dev-platform")
    (rule,) = policy["spec"]["ingress"]
    (peer,) = rule["from"]
    assert peer["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "activity-worker"}}
    assert {
        "key": "agentic-dev-platform/namespace-role",
        "operator": "In",
        "values": ["worker"],
    } in (peer["namespaceSelector"]["matchExpressions"])
    targets = _named(documents, "NetworkPolicy", "load-balancer-targets", "agentic-dev-platform")
    assert "slack-poster" not in targets["spec"]["podSelector"]["matchExpressions"][0]["values"]
