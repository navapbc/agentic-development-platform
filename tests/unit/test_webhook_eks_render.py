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
        "GITHUB_WEBHOOK_SECRET_BETA": f"{ARN}/beta/github_webhook_secret",
        "JIRA_WEBHOOK_SECRET_BETA": f"{ARN}/beta/jira_webhook_secret",
    }
    assert _webhook_env(documents)["TENANT_CONFIG_BUCKET"] == "agentic-platform-tenant-config-abc"
    assert skipped == []
    worker = _named(documents, "SecretProviderClass", "activity-worker", "tenant-beta")
    worker_names = [
        entry["objectName"] for entry in json.loads(worker["spec"]["parameters"]["objects"])
    ]
    assert not [name for name in worker_names if "webhook" in name]


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


def test_a_single_tenant_webhook_keeps_the_platform_secrets(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp")]

    documents, _ = eks_render.render(overlay, inputs, REPOSITORY)

    assert {entry["objectAlias"] for entry in _webhook_objects(documents)} == {
        "database-platform-app",
        "TEMPORAL_TLS_CA",
        "GITHUB_WEBHOOK_SECRET",
        "JIRA_WEBHOOK_SECRET",
    }
    assert "TENANT_CONFIG_BUCKET" not in _webhook_env(documents)


def test_tenants_in_different_config_buckets_are_refused(overlay) -> None:
    inputs = _inputs()
    inputs["tenants"] = [_with_webhook_secrets("adp"), _with_webhook_secrets("beta")]
    inputs["tenants"][1]["config_bucket"] = "another-bucket"

    with pytest.raises(eks_render.RenderError, match="one config bucket"):
        eks_render.render(overlay, inputs, REPOSITORY)


def test_the_eks_webhook_aliases_match_the_names_the_webhook_reads() -> None:
    from services.orchestration.webhook.tenants import (
        GITHUB_SIGNING_ENV_PREFIX,
        JIRA_SIGNING_ENV_PREFIX,
        secret_env_name,
    )

    assert webhook_eks_render.TENANT_WEBHOOK_KEYS == {
        "github_webhook_secret": GITHUB_SIGNING_ENV_PREFIX,
        "jira_webhook_secret": JIRA_SIGNING_ENV_PREFIX,
    }
    assert webhook_eks_render.webhook_alias("x", "beta") == secret_env_name("x", "beta")
