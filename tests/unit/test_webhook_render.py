from __future__ import annotations

import base64
from pathlib import Path

import pytest
from scripts import webhook_render
from scripts.webhook_render import webhook_secret_values
from tests.unit.test_k8s_cluster import ADP_ENV, _beta_files, _in, _rendered

BETA_SETTINGS = (
    "GITHUB_TOKEN=beta-token\nREBAR_PROJECT=BETA\nMCP_GATEWAY_TOKEN=beta-gateway-token\n"
    "GITHUB_WEBHOOK_SECRET=beta-github-signing\nJIRA_WEBHOOK_SECRET=beta-jira-signing\n"
)
BETA_JIRA = "config_version: 1\nallowlist:\n  - BETA\n"
WEBHOOK_ENV = {
    "GITHUB_WEBHOOK_SECRET": "adp-github-signing",
    "JIRA_WEBHOOK_SECRET": "adp-jira-signing",
}


def _two_tenants(tmp_path: Path, beta_settings: str = BETA_SETTINGS) -> list[dict]:
    files = _beta_files(**{"tenants/beta.env": beta_settings, "tenants/beta/jira.yaml": BETA_JIRA})
    return _rendered(tmp_path, files, {**ADP_ENV, **WEBHOOK_ENV}, tenants="adp,beta", tenant="adp")


def _webhook(documents: list[dict]) -> dict:
    found = _in(documents, "Deployment", "agentic-dev-platform", webhook_render.WEBHOOK)
    assert found is not None
    return found


def _decoded(document: dict) -> dict[str, str]:
    return {key: base64.b64decode(value).decode() for key, value in document["data"].items()}


def test_the_webhook_receives_only_each_tenants_webhook_secrets(tmp_path: Path) -> None:
    documents = _two_tenants(tmp_path)

    found = _in(documents, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV)
    assert found is not None
    assert _decoded(found) == {
        "GITHUB_WEBHOOK_SECRET_ADP": "adp-github-signing",
        "JIRA_WEBHOOK_SECRET_ADP": "adp-jira-signing",
        "GITHUB_WEBHOOK_SECRET_BETA": "beta-github-signing",
        "JIRA_WEBHOOK_SECRET_BETA": "beta-jira-signing",
    }
    container = _webhook(documents)["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "TENANTS", "value": "adp,beta"} in container["env"]
    assert {"secretRef": {"name": webhook_render.WEBHOOK_TENANT_ENV}} in container["envFrom"]
    rendered = str(_webhook(documents)) + str(found)
    for withheld in ("beta-token", "adp-token", "beta-gateway-token", "REBAR_PROJECT"):
        assert withheld not in rendered


def test_the_webhook_reads_only_the_routing_files_of_each_bundle(tmp_path: Path) -> None:
    documents = _two_tenants(tmp_path)

    found = _in(documents, "ConfigMap", "agentic-dev-platform", webhook_render.WEBHOOK_CONFIG)
    assert found is not None
    assert set(found["data"]) == {"beta.repositories.yaml", "beta.jira.yaml"}
    volume = next(
        v
        for v in _webhook(documents)["spec"]["template"]["spec"]["volumes"]
        if v["name"] == webhook_render.INTEGRATION_CONFIG
    )
    assert volume["projected"]["sources"] == [
        {"configMap": {"name": "integration-config"}},
        {
            "configMap": {
                "name": webhook_render.WEBHOOK_CONFIG,
                "items": [
                    {"key": "beta.jira.yaml", "path": "tenants/beta/jira.yaml"},
                    {"key": "beta.repositories.yaml", "path": "tenants/beta/repositories.yaml"},
                ],
            }
        },
    ]


def test_a_new_tenant_secret_restarts_the_webhook(tmp_path: Path) -> None:
    def digest(directory: str, settings: str) -> str:
        documents = _two_tenants(tmp_path / directory, settings)
        annotations = _webhook(documents)["spec"]["template"]["metadata"]["annotations"]
        return annotations[webhook_render.WEBHOOK_TENANT_ENV_HASH]

    rotated = BETA_SETTINGS.replace("beta-jira-signing", "beta-jira-rotated")

    assert digest("a", BETA_SETTINGS) == digest("b", BETA_SETTINGS)
    assert digest("a2", BETA_SETTINGS) != digest("c", rotated)


def test_a_tenant_without_its_own_secret_gets_none() -> None:
    values = webhook_secret_values(["adp", "beta"], "adp", {"adp": None, "beta": {}}, {})

    assert values == {
        "GITHUB_WEBHOOK_SECRET_ADP": "local-demo-secret",
        "JIRA_WEBHOOK_SECRET_ADP": "local-demo-secret",
    }


def test_two_tenants_may_not_share_a_webhook_secret() -> None:
    with pytest.raises(ValueError, match="share a GITHUB_WEBHOOK_SECRET"):
        webhook_secret_values(
            ["adp", "beta"],
            "adp",
            {"adp": None, "beta": {"GITHUB_WEBHOOK_SECRET": "same"}},
            {"GITHUB_WEBHOOK_SECRET": "same"},
        )


def test_a_single_tenant_webhook_serves_its_tenant_with_the_platform_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    localized = _rendered(tmp_path / "a", env={**ADP_ENV, **WEBHOOK_ENV}, tenant="adp")
    monkeypatch.setattr(webhook_render, "localize_webhook", lambda documents, *_: documents)
    before = _rendered(tmp_path / "b", env={**ADP_ENV, **WEBHOOK_ENV}, tenant="adp")

    container = _webhook(localized)["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "TENANTS", "value": "adp"} in container["env"]
    assert "envFrom" not in container
    assert (
        _in(localized, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV) is None
    )
    expected = _webhook(before)["spec"]["template"]["spec"]["containers"][0]
    expected["env"].append({"name": "TENANTS", "value": "adp"})
    assert localized == before
