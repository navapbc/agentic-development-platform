"""A government tenant's Slack settings name only approved hosts and minimal content, and every
tenant's response_url must belong to the hooks host of its Slack API."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from scripts import config_check
from scripts.config import get_settings
from services.integration import tenant_config
from services.integration.credentials import ConfigError
from services.integration.slack.policy import response_url_allowed
from services.integration.tenant_config import BundleStore, DirectorySource, TenantConfig
from services.orchestration import notifications
from services.orchestration.escalation import (
    EscalationConfig,
    SlackSettings,
    parse_escalation_config,
)
from services.orchestration.tenant_pinning import start_tenant_config
from services.orchestration.webhook.server import TenantSlackSettings
from services.orchestration.webhook.tenants import TenantRouting
from tests.support import hermetic_settings, write_image_policy
from tests.unit.test_tenant_config import GATES, REPOSITORIES, TICKETS

REPOSITORY = Path(__file__).resolve().parents[2]
GOVERNMENT_GATES = GATES.replace("profile: private", "profile: government")
GOVSLACK = """config_version: 1
channels: [jira, slack]
slack:
  transport: webhook
  channel: C0123456789
  organization_id: T0123456789
  app_id: A0123456789
  profile: government
  content: minimal
  api_base_url: https://slack-gov.com/api/
  approved_hosts: [slack-gov.com, hooks.slack-gov.com]
"""
UNAPPROVED_HOST = GOVSLACK.replace("https://slack-gov.com/api/", "https://slack.com/api/")
FULL_CONTENT = GOVSLACK.replace("content: minimal", "content: full")
PRIVATE = GOVSLACK.replace("profile: government", "profile: private").replace(
    "https://slack-gov.com/api/", "https://slack.example.net/api/"
)


def _config(text: str) -> EscalationConfig:
    return parse_escalation_config(yaml.safe_load(text))


def _slack(text: str) -> SlackSettings:
    slack = _config(text).slack
    assert slack is not None
    return slack


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Iterator[Path]:
    env = tmp_path / "env"
    env.mkdir()
    (env / "repositories.yaml").write_text(REPOSITORIES)
    (env / "gates.yaml").write_text(GOVERNMENT_GATES)
    (env / "tickets.yaml").write_text(TICKETS)
    write_image_policy(env)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings():
    return get_settings().model_copy(
        update={"tenant": "adp", "ticket_backend": "rebar", "tenant_config_bucket": ""}
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (UNAPPROVED_HOST, "slack.api_base_url names host slack.com"),
        (FULL_CONTENT, "slack.content is full"),
    ],
)
def test_a_noncompliant_government_tenant_is_refused_when_parsed(text, reason) -> None:
    with pytest.raises(ConfigError, match=reason):
        _config(text)


def test_a_compliant_government_tenant_accepts_only_govslack_action_urls() -> None:
    slack = _slack(GOVSLACK)

    assert response_url_allowed("https://hooks.slack-gov.com/actions/T1/1/abc", slack)
    assert not response_url_allowed("https://hooks.slack.com/actions/T1/1/abc", slack)
    assert not response_url_allowed("http://hooks.slack-gov.com/actions/T1/1/abc", slack)


def test_a_government_tenant_refuses_a_hooks_host_it_did_not_approve() -> None:
    slack = _slack(
        GOVSLACK.replace(
            "approved_hosts: [slack-gov.com, hooks.slack-gov.com]",
            "approved_hosts: [slack-gov.com]",
        )
    )

    assert not response_url_allowed("https://hooks.slack-gov.com/actions/T1/1/abc", slack)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://hooks.slack.com/actions/T1/1/abc", True),
        ("http://hooks.slack.com/actions/T1/1/abc", False),
        ("https://hooks.slack.com.evil.example/actions/T1/1/abc", False),
        ("https://hooks.slack-gov.com/actions/T1/1/abc", False),
        ("https://hooks.slack.com/services/T1/1/abc", False),
        ("https://hooks.slack.com/actions", False),
    ],
)
def test_a_private_tenant_with_any_api_host_accepts_only_slack_action_urls(url, allowed) -> None:
    assert response_url_allowed(url, _slack(PRIVATE)) is allowed


def test_config_check_reports_a_government_tenant_with_an_unapproved_host(
    environment, capsys
) -> None:
    (environment / "escalation.yaml.example").write_text(
        (REPOSITORY / "config/local/escalation.yaml.example").read_text()
    )
    (environment / "escalation.yaml").write_text(UNAPPROVED_HOST)

    assert config_check.main() == 1

    output = capsys.readouterr().out
    assert "[config-check] escalation: INVALID, slack.api_base_url names host slack.com" in output


def test_a_worker_refuses_to_start_for_a_government_tenant_with_an_unapproved_host(
    environment, monkeypatch
) -> None:
    monkeypatch.setattr(notifications, "deliver_alert", lambda message: None)
    bundle = environment / "tenants" / "adp"
    bundle.mkdir(parents=True)
    (bundle / "escalation.yaml").write_text(UNAPPROVED_HOST)

    with pytest.raises(ConfigError, match="slack.api_base_url names host slack.com"):
        start_tenant_config(_settings())


def test_a_refresh_to_an_unapproved_host_keeps_the_last_compliant_bundle(
    environment, tmp_path
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "escalation.yaml").write_text(GOVSLACK)
    rejected: list[str] = []
    config = TenantConfig(
        _settings(),
        DirectorySource(source_dir, "adp"),
        BundleStore(tmp_path / "store", "adp"),
        on_rejected=lambda identity, reason: rejected.append(reason),
    )
    assert config.refresh() is True
    first = config.active

    (source_dir / "escalation.yaml").write_text(UNAPPROVED_HOST)

    assert config.refresh() is False
    assert config.active == first
    assert "slack.api_base_url names host slack.com" in rejected[0]


def test_the_webhook_keeps_a_tenants_last_compliant_slack_settings_and_serves_the_others(
    environment,
) -> None:
    beta = PRIVATE.replace("A0123456789", "A0BETA")
    tenants = {"adp": GOVSLACK, "beta": beta, "gamma": UNAPPROVED_HOST, "delta": PRIVATE}
    for tenant, text in tenants.items():
        (environment / "tenants" / tenant).mkdir(parents=True)
        (environment / "tenants" / tenant / "escalation.yaml").write_text(text)
    (environment / "tenants" / "beta" / "gates.yaml").write_text(GATES)
    settings = hermetic_settings(tenants=",".join(tenants))
    slack = TenantSlackSettings(settings, TenantRouting(settings))
    slack.refresh()
    first = slack.slack("adp")
    assert first is not None and first.api_base_url == "https://slack-gov.com/api/"
    assert slack.slack("gamma") is None
    assert slack.slack("delta") is None

    (environment / "tenants" / "adp" / "escalation.yaml").write_text(UNAPPROVED_HOST)
    slack.refresh()

    assert slack.slack("adp") == first
    served = slack.slack("beta")
    assert served is not None and served.profile == "private"
