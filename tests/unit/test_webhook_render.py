from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
from scripts import k8s_render, webhook_eks_render, webhook_render
from scripts.tenant_render import review_namespace
from scripts.webhook_render import webhook_secret_values
from services.slack_poster import PORT, SERVICE_ACCOUNT_AUDIENCE
from tests.support import SHARED_APP_YAML
from tests.unit.test_k8s_cluster import ADP_ENV, _beta_files, _in, _rendered, _worker

BETA_SETTINGS = (
    "GITHUB_TOKEN=beta-token\nREBAR_PROJECT=BETA\nMCP_GATEWAY_TOKEN=beta-gateway-token\n"
    "GITHUB_WEBHOOK_SECRET=beta-github-signing\nJIRA_WEBHOOK_SECRET=beta-jira-signing\n"
)
BETA_JIRA = "config_version: 1\nallowlist:\n  - BETA\n"
ADP_SETTINGS = {
    **ADP_ENV,
    "GITHUB_WEBHOOK_SECRET": "adp-github-signing",
    "JIRA_WEBHOOK_SECRET": "adp-jira-signing",
}


def _two_tenants(tmp_path: Path, beta_settings: str = BETA_SETTINGS) -> list[dict]:
    files = _beta_files(**{"tenants/beta.env": beta_settings, "tenants/beta/jira.yaml": BETA_JIRA})
    return _rendered(tmp_path, files, adp=ADP_SETTINGS)


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
    assert set(found["data"]) == {
        "adp.repositories.yaml",
        "adp.jira.yaml",
        "beta.repositories.yaml",
        "beta.jira.yaml",
    }
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
                    {"key": "adp.jira.yaml", "path": "tenants/adp/jira.yaml"},
                    {"key": "adp.repositories.yaml", "path": "tenants/adp/repositories.yaml"},
                    {"key": "beta.jira.yaml", "path": "tenants/beta/jira.yaml"},
                    {"key": "beta.repositories.yaml", "path": "tenants/beta/repositories.yaml"},
                ],
            }
        },
    ]


def test_each_tenants_integrations_file_reaches_the_webhook(tmp_path: Path) -> None:
    modes = "config_version: 1\nmodes:\n  jira_intake: webhook\n"
    files = _beta_files(
        **{
            "tenants/beta.env": BETA_SETTINGS,
            "tenants/beta/jira.yaml": BETA_JIRA,
            "tenants/beta/integrations.yaml": modes,
        }
    )
    documents = _rendered(tmp_path, files, adp=ADP_SETTINGS)

    found = _in(documents, "ConfigMap", "agentic-dev-platform", webhook_render.WEBHOOK_CONFIG)
    assert found is not None
    assert found["data"]["beta.integrations.yaml"] == modes
    items = next(
        source["configMap"]["items"]
        for v in _webhook(documents)["spec"]["template"]["spec"]["volumes"]
        if v["name"] == webhook_render.INTEGRATION_CONFIG
        for source in v["projected"]["sources"]
        if source["configMap"]["name"] == webhook_render.WEBHOOK_CONFIG
    )
    assert {"key": "beta.integrations.yaml", "path": "tenants/beta/integrations.yaml"} in items


def test_a_new_tenant_secret_restarts_the_webhook(tmp_path: Path) -> None:
    def digest(directory: str, settings: str) -> str:
        documents = _two_tenants(tmp_path / directory, settings)
        annotations = _webhook(documents)["spec"]["template"]["metadata"]["annotations"]
        return annotations[webhook_render.WEBHOOK_TENANT_ENV_HASH]

    rotated = BETA_SETTINGS.replace("beta-jira-signing", "beta-jira-rotated")

    assert digest("a", BETA_SETTINGS) == digest("b", BETA_SETTINGS)
    assert digest("a2", BETA_SETTINGS) != digest("c", rotated)


def test_a_tenant_without_its_own_secret_gets_none() -> None:
    values = webhook_secret_values(
        ["adp", "beta"], {"adp": {"GITHUB_WEBHOOK_SECRET": "adp-github"}, "beta": {}}
    )

    assert values == {"GITHUB_WEBHOOK_SECRET_ADP": "adp-github"}


def test_two_tenants_may_not_share_a_webhook_secret() -> None:
    with pytest.raises(ValueError, match="share a GITHUB_WEBHOOK_SECRET"):
        webhook_secret_values(
            ["adp", "beta"],
            {"adp": {"GITHUB_WEBHOOK_SECRET": "same"}, "beta": {"GITHUB_WEBHOOK_SECRET": "same"}},
        )


def test_a_single_tenant_webhook_serves_its_tenant_with_its_own_secrets(tmp_path: Path) -> None:
    localized = _rendered(tmp_path, adp=ADP_SETTINGS)

    container = _webhook(localized)["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "TENANTS", "value": "adp"} in container["env"]
    assert {"secretRef": {"name": webhook_render.WEBHOOK_TENANT_ENV}} in container["envFrom"]
    secrets = _in(localized, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV)
    assert secrets is not None
    assert _decoded(secrets) == {
        "GITHUB_WEBHOOK_SECRET_ADP": "adp-github-signing",
        "JIRA_WEBHOOK_SECRET_ADP": "adp-jira-signing",
    }
    found = _in(localized, "ConfigMap", "agentic-dev-platform", webhook_render.WEBHOOK_CONFIG)
    assert found is not None
    assert set(found["data"]) == {"adp.jira.yaml", "adp.repositories.yaml"}


ESCALATION = "config_version: 1\nchannels: [jira, email]\n"


def _routing_projection(documents: list[dict]) -> list[dict]:
    volume = next(
        v
        for v in _webhook(documents)["spec"]["template"]["spec"]["volumes"]
        if v["name"] == webhook_render.INTEGRATION_CONFIG
    )
    return volume["projected"]["sources"]


def test_a_single_tenant_webhook_reads_its_tenants_escalation_file(tmp_path: Path) -> None:
    documents = _rendered(tmp_path, {"escalation.yaml": ESCALATION}, adp=ADP_SETTINGS)

    found = _in(documents, "ConfigMap", "agentic-dev-platform", webhook_render.WEBHOOK_CONFIG)
    assert found is not None
    assert found["data"]["adp.escalation.yaml"] == ESCALATION
    assert "escalation.yaml" not in found["data"]
    assert {"key": "adp.escalation.yaml", "path": "tenants/adp/escalation.yaml"} in (
        _routing_projection(documents)[1]["configMap"]["items"]
    )


def test_each_tenant_bundles_escalation_file_reaches_the_webhook(tmp_path: Path) -> None:
    files = _beta_files(
        **{
            "tenants/beta.env": BETA_SETTINGS,
            "tenants/beta/jira.yaml": BETA_JIRA,
            "tenants/beta/escalation.yaml": ESCALATION,
            "escalation.yaml": ESCALATION,
        }
    )
    documents = _rendered(tmp_path, files, adp=ADP_SETTINGS)

    assert {"key": "beta.escalation.yaml", "path": "tenants/beta/escalation.yaml"} in (
        _routing_projection(documents)[1]["configMap"]["items"]
    )
    assert {"key": "adp.escalation.yaml", "path": "tenants/adp/escalation.yaml"} in (
        _routing_projection(documents)[1]["configMap"]["items"]
    )


def test_the_webhook_receives_each_tenants_slack_signing_secret_and_action_key(
    tmp_path: Path,
) -> None:
    from tests.unit.test_k8s_cluster import BETA_SLACK, SLACK_ENV

    files = _beta_files(**{"tenants/beta.env": BETA_SLACK, "tenants/beta/jira.yaml": BETA_JIRA})
    documents = _rendered(tmp_path, files, adp={**ADP_SETTINGS, **SLACK_ENV})

    found = _in(documents, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV)
    assert found is not None
    values = _decoded(found)
    assert {key: value for key, value in values.items() if "SLACK" in key or "ACTION" in key} == {
        "SLACK_SIGNING_SECRET_ADP": "adp-slack-signing",
        "SLACK_SIGNING_SECRET_BETA": "beta-slack-signing",
        "ESCALATION_ACTION_KEY_ADP": "adp-action-key",
        "ESCALATION_ACTION_KEY_BETA": "beta-action-key",
    }
    assert "xoxb" not in str(values) and "xapp" not in str(values)


def test_a_tenant_without_a_slack_signing_secret_gets_none() -> None:
    values = webhook_secret_values(["adp", "beta"], {"adp": None, "beta": {}})

    assert not any(
        key.startswith(("SLACK_SIGNING_SECRET", "ESCALATION_ACTION_KEY")) for key in values
    )


def test_two_tenants_may_not_share_a_slack_signing_secret() -> None:
    with pytest.raises(ValueError, match="share a SLACK_SIGNING_SECRET"):
        webhook_secret_values(
            ["adp", "beta"],
            {"adp": {"SLACK_SIGNING_SECRET": "same"}, "beta": {"SLACK_SIGNING_SECRET": "same"}},
        )


SHARED_APP = SHARED_APP_YAML
SHARED_ESCALATION = (
    "config_version: 1\nchannels: [jira, slack]\nslack:\n  app: shared\n  transport: webhook\n"
    "  channel: C0BETA0001\n  profile: private\n"
)
SHARED_ENV = {
    "SLACK_SHARED_BOT_TOKEN": "xoxb-shared",
    "SLACK_SHARED_SIGNING_SECRET": "shared-signing",
}


def _shared_tenants(tmp_path: Path, **render: Any) -> list[dict]:
    files = _beta_files(
        **{
            "tenants/beta.env": BETA_SETTINGS,
            "tenants/beta/jira.yaml": BETA_JIRA,
            "tenants/beta/escalation.yaml": SHARED_ESCALATION,
            "slack-shared-app.yaml": SHARED_APP,
        }
    )
    return _rendered(tmp_path, files, SHARED_ENV, adp=ADP_SETTINGS, **render)


def test_without_a_shared_app_the_cluster_runs_no_slack_poster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    (deployed / "slack-shared-app.yaml").write_text(SHARED_APP)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(deployed))
    documents = _two_tenants(tmp_path / "checkout")

    assert not [
        document
        for document in documents
        if (document["kind"], document["metadata"]["name"])
        in webhook_eks_render.SLACK_POSTER_OBJECTS
    ]


def test_the_shared_apps_bot_token_reaches_only_the_slack_poster(tmp_path: Path) -> None:
    documents = _shared_tenants(tmp_path)

    poster = _in(documents, "Deployment", "agentic-dev-platform", webhook_render.SLACK_POSTER)
    assert poster is not None
    assert poster["spec"]["replicas"] == 1
    assert poster["spec"]["strategy"] == {"type": "Recreate"}
    container = poster["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "TENANTS", "value": "adp,beta"} in container["env"]
    token = _in(documents, "Secret", "agentic-dev-platform", webhook_render.SLACK_POSTER)
    assert _decoded(token) == {"slack_shared_bot_token": "xoxb-shared"}
    for document in documents:
        if document["kind"] != "Secret" or document is token:
            continue
        values = _decoded(document).values()
        assert "xoxb-shared" not in values


def test_the_slack_poster_rolls_on_its_own_token_hash(tmp_path: Path) -> None:
    documents = _shared_tenants(tmp_path)

    poster = _in(documents, "Deployment", "agentic-dev-platform", webhook_render.SLACK_POSTER)
    annotations = poster["spec"]["template"]["metadata"]["annotations"]
    assert "agentic-dev-platform/slack-poster-token-hash" in annotations
    assert webhook_render.WEBHOOK_TENANT_ENV_HASH not in annotations


def test_the_shared_apps_signing_secret_reaches_only_the_webhook(tmp_path: Path) -> None:
    documents = _shared_tenants(tmp_path)

    webhook_secret = _in(
        documents, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV
    )
    assert _decoded(webhook_secret)["SLACK_SHARED_SIGNING_SECRET"] == "shared-signing"
    for document in documents:
        if document["kind"] == "Secret" and document is not webhook_secret:
            assert "shared-signing" not in _decoded(document).values()


def test_the_shared_apps_app_token_reaches_only_the_webhook(tmp_path: Path) -> None:
    files = _beta_files(
        **{
            "tenants/beta.env": BETA_SETTINGS,
            "tenants/beta/jira.yaml": BETA_JIRA,
            "tenants/beta/escalation.yaml": SHARED_ESCALATION,
            "slack-shared-app.yaml": SHARED_APP,
        }
    )
    env = {**SHARED_ENV, "SLACK_SHARED_APP_TOKEN": "xapp-shared"}
    documents = _rendered(tmp_path, files, env, adp=ADP_SETTINGS)

    webhook_secret = _in(
        documents, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV
    )
    assert _decoded(webhook_secret)["SLACK_SHARED_APP_TOKEN"] == "xapp-shared"
    for document in documents:
        if document["kind"] == "Secret" and document is not webhook_secret:
            assert "xapp-shared" not in _decoded(document).values()


def test_a_tenant_file_may_not_carry_the_shared_apps_app_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sets SLACK_SHARED_APP_TOKEN"):
        _two_tenants(tmp_path, BETA_SETTINGS + "SLACK_SHARED_APP_TOKEN=xapp-shared\n")


def test_the_webhook_and_the_slack_poster_read_the_shared_app_and_each_escalation_file(
    tmp_path: Path,
) -> None:
    documents = _shared_tenants(tmp_path)

    for name in (webhook_render.WEBHOOK, webhook_render.SLACK_POSTER):
        deployment = _in(documents, "Deployment", "agentic-dev-platform", name)
        volume = next(
            v
            for v in deployment["spec"]["template"]["spec"]["volumes"]
            if v["name"] == webhook_render.INTEGRATION_CONFIG
        )
        items = volume["projected"]["sources"][1]["configMap"]["items"]
        assert {"key": "slack-shared-app.yaml", "path": "slack-shared-app.yaml"} in items
        assert {"key": "beta.escalation.yaml", "path": "tenants/beta/escalation.yaml"} in items


def test_a_tenant_file_may_not_carry_a_shared_slack_secret(tmp_path: Path) -> None:
    settings = BETA_SETTINGS + "SLACK_SHARED_BOT_TOKEN=xoxb-shared\n"
    with pytest.raises(ValueError, match="sets SLACK_SHARED_BOT_TOKEN"):
        _two_tenants(tmp_path, settings)


def test_every_tenant_worker_can_reach_the_slack_poster_with_a_projected_token(
    tmp_path: Path,
) -> None:
    documents = _shared_tenants(tmp_path)

    for tenant in ("adp", "beta"):
        worker = _in(documents, "Deployment", f"tenant-{tenant}", "activity-worker")
        spec = worker["spec"]["template"]["spec"]
        volume = next(v for v in spec["volumes"] if v["name"] == "slack-posting-token")
        assert volume["projected"]["sources"] == [
            {
                "serviceAccountToken": {
                    "audience": SERVICE_ACCOUNT_AUDIENCE,
                    "expirationSeconds": 600,
                    "path": "token",
                }
            }
        ]
        env = {e["name"]: e.get("value") for e in spec["containers"][-1]["env"]}
        assert env["SLACK_POSTING_URL"] == (
            f"https://slack-poster.agentic-dev-platform.svc.cluster.local:{PORT}"
        )


def test_the_slack_poster_receives_each_action_key_fingerprint_and_no_key(
    tmp_path: Path,
) -> None:
    files = _beta_files(
        **{
            "tenants/beta.env": BETA_SETTINGS + "ESCALATION_ACTION_KEY=beta-action-key\n",
            "tenants/beta/jira.yaml": BETA_JIRA,
            "tenants/beta/escalation.yaml": SHARED_ESCALATION,
            "slack-shared-app.yaml": SHARED_APP,
        }
    )
    adp = {**ADP_SETTINGS, "ESCALATION_ACTION_KEY": "adp-action-key"}
    documents = _rendered(tmp_path, files, SHARED_ENV, adp=adp)

    webhook_secrets = _decoded(
        _in(documents, "Secret", "agentic-dev-platform", webhook_render.WEBHOOK_TENANT_ENV)
    )
    poster = _in(documents, "Deployment", "agentic-dev-platform", webhook_render.SLACK_POSTER)
    env = {
        e["name"]: e.get("value")
        for e in poster["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert {name: value for name, value in env.items() if "ACTION_KEY" in name} == {
        f"ESCALATION_ACTION_KEY_SHA256_{tenant}": hashlib.sha256(
            webhook_secrets[f"ESCALATION_ACTION_KEY_{tenant}"].encode()
        ).hexdigest()
        for tenant in ("ADP", "BETA")
    }
    poster_secret = _in(documents, "Secret", "agentic-dev-platform", webhook_render.SLACK_POSTER)
    assert set(_decoded(poster_secret)) == {webhook_render.SHARED_BOT_CREDENTIAL_KEY}


def test_only_tenant_activity_workers_and_platform_callers_reach_the_local_slack_poster(
    tmp_path: Path,
) -> None:
    documents = _shared_tenants(tmp_path)

    policy = _in(documents, "NetworkPolicy", "agentic-dev-platform", "slack-poster-ingress")
    assert policy is not None
    assert policy["spec"]["podSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": webhook_render.SLACK_POSTER}
    }
    [rule] = policy["spec"]["ingress"]
    peer, platform_peer = rule["from"]
    assert peer["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "activity-worker"}}
    assert platform_peer == {
        "podSelector": {
            "matchExpressions": [
                {
                    "key": "app.kubernetes.io/name",
                    "operator": "In",
                    "values": ["inbound-webhook", "deployment-validate"],
                }
            ]
        }
    }
    admitted = {
        document["metadata"]["name"]
        for document in documents
        if document["kind"] == "Namespace"
        and all(
            _matches(document["metadata"].get("labels", {}), expression)
            for expression in peer["namespaceSelector"]["matchExpressions"]
        )
    }
    assert admitted == {"tenant-adp", "tenant-beta"}
    assert rule["ports"] == [{"protocol": "TCP", "port": PORT}]


def _matches(labels: dict[str, str], expression: dict[str, Any]) -> bool:
    if expression["operator"] == "Exists":
        return expression["key"] in labels
    return labels.get(expression["key"]) in expression["values"]


def test_no_role_in_a_tenant_worker_namespace_can_create_pods_or_tokens(tmp_path: Path) -> None:
    documents = _shared_tenants(tmp_path)

    for document in documents:
        if document["kind"] != "Role" or not document["metadata"]["namespace"].startswith(
            "tenant-"
        ):
            continue
        if document["metadata"]["namespace"].endswith("-sandbox"):
            continue
        for rule in document.get("rules", []):
            resources = set(rule.get("resources", []))
            assert not ({"pods", "serviceaccounts/token"} & resources and "create" in rule["verbs"])


def test_every_tenant_worker_verifies_the_slack_poster_against_the_local_ca(
    tmp_path: Path,
) -> None:
    from cryptography import x509

    documents = _shared_tenants(tmp_path)

    ca = _in(documents, "ConfigMap", "tenant-beta", "slack-poster-ca")
    assert ca is not None
    worker = _in(documents, "Deployment", "tenant-beta", "activity-worker")
    container = worker["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "SLACK_POSTING_CA_FILE", "value": "/etc/slack-poster-ca/ca.pem"} in (
        container["env"]
    )
    poster = _in(documents, "Deployment", "agentic-dev-platform", webhook_render.SLACK_POSTER)
    spec = poster["spec"]["template"]["spec"]
    assert {
        "name": "slack-poster-tls",
        "secret": {"secretName": "slack-poster-tls", "defaultMode": 0o440},
    } in spec["volumes"]
    tls = _in(documents, "Secret", "agentic-dev-platform", "slack-poster-tls")
    leaf = x509.load_pem_x509_certificate(_decoded(tls)["tls.crt"].encode())
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(name.value) for name in sans] == [
        "slack-poster.agentic-dev-platform.svc.cluster.local",
        "127.0.0.1",
    ]
    annotations = poster["spec"]["template"]["metadata"]["annotations"]
    assert webhook_render.SLACK_POSTER_TLS_FINGERPRINT in annotations


PLATFORM_ESCALATION = "config_version: 1\nadministrators:\n  email:\n    - admins@platform.test\n"


def _config_volume(document: dict, name: str) -> dict:
    return next(v for v in document["spec"]["template"]["spec"]["volumes"] if v["name"] == name)


def test_every_pod_that_reads_platform_escalation_receives_it(tmp_path: Path) -> None:
    documents = _shared_tenants(tmp_path, platform_escalation=PLATFORM_ESCALATION)
    entry = {"key": "platform-escalation.yaml", "path": "platform-escalation.yaml"}

    for namespace in ("tenant-adp", "tenant-beta", review_namespace("adp")):
        config = _in(documents, "ConfigMap", namespace, k8s_render.CONFIG_VOLUME)
        assert config is not None
        assert config["data"]["platform-escalation.yaml"] == PLATFORM_ESCALATION
    for namespace, name in (
        ("tenant-adp", "activity-worker"),
        ("tenant-beta", "activity-worker"),
        (review_namespace("adp"), "review-worker"),
    ):
        workload = _in(documents, "Deployment", namespace, name)
        assert workload is not None
        sources = _config_volume(workload, k8s_render.CONFIG_VOLUME)["projected"]["sources"]
        assert {"configMap": {"name": k8s_render.CONFIG_VOLUME}} in sources
    routed = _in(documents, "ConfigMap", "agentic-dev-platform", webhook_render.WEBHOOK_CONFIG)
    assert routed is not None
    assert routed["data"]["platform-escalation.yaml"] == PLATFORM_ESCALATION
    for name in (webhook_render.WEBHOOK, webhook_render.SLACK_POSTER):
        deployment = _in(documents, "Deployment", "agentic-dev-platform", name)
        assert deployment is not None
        volume = _config_volume(deployment, webhook_render.INTEGRATION_CONFIG)
        assert entry in volume["projected"]["sources"][1]["configMap"]["items"]
    cronjob = _in(documents, "CronJob", "agentic-dev-platform", "deployment-validate")
    assert cronjob is not None
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == webhook_render.INTEGRATION_CONFIG)
    assert volume["configMap"]["name"] == webhook_render.WEBHOOK_CONFIG
    assert entry in volume["configMap"]["items"]
    assert {
        "name": webhook_render.INTEGRATION_CONFIG,
        "mountPath": "/app/config/local",
        "readOnly": True,
    } in pod["containers"][0]["volumeMounts"]


def test_a_changed_platform_escalation_file_rolls_the_pods_that_read_it(tmp_path: Path) -> None:
    def hashes(directory: str, text: str) -> list[str]:
        documents = _rendered(tmp_path / directory, adp=ADP_SETTINGS, platform_escalation=text)
        return [
            deployment["spec"]["template"]["metadata"]["annotations"][k8s_render.CONFIG_HASH]
            for deployment in (_worker(documents, "adp"), _webhook(documents))
        ]

    first = hashes("first", PLATFORM_ESCALATION)

    assert hashes("same", PLATFORM_ESCALATION) == first
    changed = hashes("changed", PLATFORM_ESCALATION.replace("admins@", "oncall@"))
    assert all(new != old for new, old in zip(changed, first, strict=True))
