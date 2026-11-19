"""A tenant's escalation.yaml selects its alert channels and its Slack settings."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from scripts import config_check
from scripts.config import get_settings
from services.integration import tenant_config
from services.integration.credentials import ConfigError
from services.integration.tenant_config import validate_bundle
from services.orchestration.escalation import load_escalation_config
from tests.support import write_image_policy
from tests.unit.test_tenant_config import GATES, REPOSITORIES, TICKETS

REPOSITORY = Path(__file__).resolve().parents[2]
SLACK = """config_version: 1
channels: [jira, email, slack]
slack:
  transport: socket
  channel: C0123456789
  organization_id: T0123456789
  app_id: A0123456789
  profile: private
  allowlist:
    gate:
      - user_id: U0123456789
"""
GOVERNMENT_SLACK = SLACK.replace(
    "profile: private", "profile: government\n  approved_hosts: [slack.com]"
)


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Iterator[Path]:
    env = tmp_path / "env"
    env.mkdir()
    (env / "repositories.yaml").write_text(REPOSITORIES)
    (env / "gates.yaml").write_text(GATES)
    (env / "tickets.yaml").write_text(TICKETS)
    write_image_policy(env)
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings():
    return get_settings().model_copy(
        update={"tenant": "adp", "ticket_backend": "rebar", "tenant_config_bucket": ""}
    )


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir()
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


@pytest.mark.parametrize(
    "example",
    [
        "config/local/escalation.yaml.example",
        "config/local/tenants/beta.example/escalation.yaml.example",
    ],
)
def test_each_shipped_example_validates_in_a_bundle(environment, tmp_path, example) -> None:
    example = REPOSITORY / example

    validate_bundle(_bundle(tmp_path, {"escalation.yaml": example.read_text()}), _settings())


def test_a_misspelled_escalation_file_is_refused(environment, tmp_path) -> None:
    with pytest.raises(ConfigError, match="escalations.yaml is not a tenant config file"):
        validate_bundle(_bundle(tmp_path, {"escalations.yaml": SLACK}), _settings())


@pytest.mark.parametrize(
    ("text", "field"),
    [
        (SLACK.replace("transport: socket", "transport: hybrid"), "slack.transport"),
        (SLACK.replace("  channel: C0123456789\n", ""), "slack.channel"),
        (SLACK.replace("  organization_id: T0123456789\n", ""), "slack.organization_id"),
        (
            SLACK.replace("- user_id: U0123456789", "- organization_id: T0123456789"),
            r"slack\.allowlist\.gate\[0\]\.user_id",
        ),
    ],
)
def test_an_invalid_slack_setting_fails_validation_with_the_field_named(
    environment, tmp_path, text, field
) -> None:
    with pytest.raises(ConfigError, match=f"escalation.yaml: {field}"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": text}), _settings())


def test_a_slack_profile_other_than_the_gates_profile_is_refused(environment, tmp_path) -> None:
    text = GOVERNMENT_SLACK

    with pytest.raises(ConfigError, match="slack.profile is government"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": text}), _settings())


def test_config_check_refuses_a_slack_profile_other_than_the_gates_profile(
    environment, capsys
) -> None:
    (environment / "escalation.yaml.example").write_text(
        (REPOSITORY / "config/local/escalation.yaml.example").read_text()
    )
    (environment / "escalation.yaml").write_text(GOVERNMENT_SLACK)

    assert config_check.main() == 1

    output = capsys.readouterr().out
    assert "[config-check] escalation: INVALID, slack.profile is government" in output


def test_config_check_reports_a_current_escalation_file(environment, capsys) -> None:
    (environment / "escalation.yaml.example").write_text(
        (REPOSITORY / "config/local/escalation.yaml.example").read_text()
    )
    (environment / "escalation.yaml").write_text(SLACK)

    config_check.main()

    assert "[config-check] escalation: up to date (version 1)" in capsys.readouterr().out


def test_an_omitted_token_lifetime_is_72_hours(environment) -> None:
    (environment / "escalation.yaml").write_text(SLACK)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert slack.token_ttl_hours == 72


@pytest.mark.parametrize(
    ("profile", "text", "content"),
    [("private", SLACK, "full"), ("government", GOVERNMENT_SLACK, "minimal")],
)
def test_the_content_level_defaults_by_profile(environment, profile, text, content) -> None:
    (environment / "gates.yaml").write_text(GATES.replace("private", profile))
    (environment / "escalation.yaml").write_text(text)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert slack.content == content
