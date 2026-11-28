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
from services.orchestration.escalation import (
    MAX_APPROVER_GROUPS,
    Approvers,
    SlackGroup,
    SlackUser,
    load_escalation_config,
)
from tests.support import (
    SHARED_APP_ID,
    SHARED_APP_YAML,
    configure_tenant,
    tenant_dir,
    write_image_policy,
)
from tests.unit.test_tenant_config import GATES, REPOSITORIES, TICKETS

REPOSITORY = Path(__file__).resolve().parents[2]
TEMPLATE = "config/local/tenants/tenant.example/escalation.yaml.example"
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
    (tenant_dir(env) / "repositories.yaml").write_text(REPOSITORIES)
    (env / "gates.yaml").write_text(GATES)
    (tenant_dir(env) / "tickets.yaml").write_text(TICKETS)
    write_image_policy(env)
    configure_tenant(monkeypatch, env)
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _copy_escalation_template(environment: Path) -> None:
    template = environment / "tenants" / "tenant.example" / "escalation.yaml.example"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text((REPOSITORY / TEMPLATE).read_text())


def _settings():
    return get_settings().model_copy(
        update={"tenant": "adp", "ticket_backend": "rebar", "tenant_config_bucket": ""}
    )


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir()
    for name, text in {"repositories.yaml": REPOSITORIES, "tickets.yaml": TICKETS, **files}.items():
        (directory / name).write_text(text)
    return directory


@pytest.mark.parametrize(
    "example",
    ["config/local/tenants/tenant.example/escalation.yaml.example"],
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
            r"slack\.allowlist\.gate\[0\] must name exactly one of user_id and group_id",
        ),
        (
            SLACK.replace("- user_id: U0123456789", "- {user_id: U01, group_id: S01}"),
            r"slack\.allowlist\.gate\[0\] must name exactly one of user_id and group_id",
        ),
        (
            SLACK.replace("- user_id: U0123456789", "- group_id: G0123456789"),
            r"slack\.allowlist\.gate\[0\]\.group_id G0123456789 is not a Slack user group ID",
        ),
        (
            SLACK.replace("    gate:\n      - user_id: U0123456789", "    gate: everyone"),
            r"slack\.allowlist\.gate must be channel or a list of users and groups",
        ),
        (
            SLACK.replace("- user_id: U0123456789", "- {user_id: U01, team_id: T01}"),
            r"slack\.allowlist\.gate\[0\]\.team_id is allowed only on a group_id entry",
        ),
        (
            SLACK.replace("- user_id: U0123456789", "- {group_id: S01, team_id: E0PARTNER01}"),
            r"slack\.allowlist\.gate\[0\]\.team_id 'E0PARTNER01' is not a Slack workspace ID",
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
    _copy_escalation_template(environment)
    (tenant_dir(environment) / "escalation.yaml").write_text(GOVERNMENT_SLACK)

    assert config_check.main() == 1

    output = capsys.readouterr().out
    assert "[config-check] tenants/adp/escalation: INVALID, slack.profile is government" in output


def test_config_check_refuses_a_tenant_only_file_in_the_platform_directory(
    environment, capsys
) -> None:
    (environment / "escalation.yaml").write_text(SLACK)

    assert config_check.main() == 1

    output = capsys.readouterr().out
    assert "escalation.yaml, which only a tenant's bundle holds" in output
    assert "config/local/tenants/<tenant>/" in output


def test_config_check_reports_a_current_escalation_file(environment, capsys) -> None:
    _copy_escalation_template(environment)
    (tenant_dir(environment) / "escalation.yaml").write_text(SLACK)

    config_check.main()

    output = capsys.readouterr().out
    assert "[config-check] tenants/adp/escalation: up to date (version 1)" in output


def test_config_check_accepts_a_shared_app_tenant_from_the_host(
    environment, monkeypatch, capsys
) -> None:
    _copy_escalation_template(environment)
    (environment / "tenants" / "adp.env").write_text("")
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(SHARED_SLACK)
    monkeypatch.delenv("TENANT", raising=False)
    monkeypatch.delenv("TENANTS", raising=False)
    get_settings.cache_clear()

    config_check.main()

    output = capsys.readouterr().out
    assert "[config-check] tenants/adp/escalation: up to date (version 1)" in output


def test_an_omitted_token_lifetime_is_72_hours(environment) -> None:
    (tenant_dir(environment) / "escalation.yaml").write_text(SLACK)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert slack.token_ttl_hours == 72


@pytest.mark.parametrize(
    ("profile", "text", "content"),
    [("private", SLACK, "full"), ("government", GOVERNMENT_SLACK, "minimal")],
)
def test_the_content_level_defaults_by_profile(environment, profile, text, content) -> None:
    (environment / "gates.yaml").write_text(GATES.replace("private", profile))
    (tenant_dir(environment) / "escalation.yaml").write_text(text)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert slack.content == content


SHARED_APP = SHARED_APP_YAML
GOVSLACK_SHARED_APP = SHARED_APP.replace("https://slack.com/api/", "https://slack-gov.com/api/")
SHARED_SLACK = """config_version: 1
channels: [jira, slack]
slack:
  app: shared
  transport: webhook
  channel: C0123456789
  profile: private
  allowlist:
    gate:
      - user_id: U0123456789
"""
GOVERNMENT_SHARED_SLACK = SHARED_SLACK.replace(
    "profile: private", "profile: government\n  approved_hosts: [slack-gov.com]"
)


def test_a_shared_app_tenant_takes_its_app_from_the_shared_app_file(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(SHARED_SLACK)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert (slack.app, slack.app_id, slack.organization_id, slack.api_host) == (
        "shared",
        "A0SHARED001",
        "E0SHARED001",
        "slack.com",
    )
    assert slack.channel == "C0123456789"
    assert slack.approvers("gate").users[0].organization_id == "E0SHARED001"


def test_an_unknown_slack_app_is_refused_naming_own_and_shared(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(
        SHARED_SLACK.replace("app: shared", "app: other")
    )

    with pytest.raises(ConfigError, match=r"own.*shared|shared.*own"):
        load_escalation_config(_settings())


def test_the_shared_app_is_refused_when_no_tenant_is_served(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(SHARED_SLACK)
    settings = _settings().model_copy(update={"tenant": "", "tenants": ""})

    with pytest.raises(ConfigError, match="neither TENANT nor TENANTS"):
        load_escalation_config(settings)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("app_id", "A0123456789"),
        ("organization_id", "T0123456789"),
        ("api_base_url", "https://slack.com/api/"),
    ],
)
def test_a_shared_app_tenant_may_not_set_the_app_settings(
    environment, tmp_path, key, value
) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    text = SHARED_SLACK.replace("  app: shared\n", f"  app: shared\n  {key}: {value}\n")

    with pytest.raises(ConfigError, match=f"escalation.yaml: slack.{key} is set"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": text}), _settings())


def test_a_shared_app_tenant_must_use_the_webhook_transport(environment, tmp_path) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    text = SHARED_SLACK.replace("transport: webhook", "transport: socket")

    with pytest.raises(ConfigError, match="escalation.yaml: slack.transport is socket"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": text}), _settings())


@pytest.mark.parametrize("channel", ["G0123", "D0123", "U0123", "W0123", "C01ab"])
def test_a_shared_app_tenant_must_name_a_public_or_private_channel(
    environment, tmp_path, channel
) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    text = SHARED_SLACK.replace("channel: C0123456789", f"channel: {channel}")

    with pytest.raises(ConfigError, match=f"escalation.yaml: slack.channel {channel} is not"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": text}), _settings())


def test_a_shared_app_tenant_is_refused_without_the_shared_app_file(environment, tmp_path) -> None:
    with pytest.raises(ConfigError, match=r"escalation.yaml: .*slack-shared-app\.yaml"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": SHARED_SLACK}), _settings())


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            GOVERNMENT_SHARED_SLACK.replace("[slack-gov.com]", "[slack.com]"),
            "names host slack-gov.com, which slack.approved_hosts does not list",
        ),
        (
            GOVERNMENT_SHARED_SLACK.replace("  app: shared\n", "  app: shared\n  content: full\n"),
            "slack.content is full",
        ),
    ],
)
def test_a_government_tenant_on_a_govslack_shared_app_keeps_the_profile_policy(
    environment, text, reason
) -> None:
    (environment / "gates.yaml").write_text(GATES.replace("private", "government"))
    (environment / "slack-shared-app.yaml").write_text(GOVSLACK_SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(text)

    with pytest.raises(ConfigError, match=reason):
        load_escalation_config(_settings())


def test_a_government_tenant_on_a_govslack_shared_app_with_its_host_approved_loads(
    environment,
) -> None:
    (environment / "gates.yaml").write_text(GATES.replace("private", "government"))
    (environment / "slack-shared-app.yaml").write_text(GOVSLACK_SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(GOVERNMENT_SHARED_SLACK)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert (slack.api_host, slack.content) == ("slack-gov.com", "minimal")


def test_a_file_without_app_parses_as_a_tenant_owned_app(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    (tenant_dir(environment) / "escalation.yaml").write_text(SLACK)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert (slack.app, slack.app_id, slack.transport) == ("own", "A0123456789", "socket")


def test_a_tenant_owned_app_may_not_declare_the_shared_app_id(environment, tmp_path) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    text = SLACK.replace("app_id: A0123456789", f"app_id: {SHARED_APP_ID}")

    with pytest.raises(ConfigError, match="escalation.yaml: slack.app_id A0SHARED001 is the ID"):
        validate_bundle(_bundle(tmp_path, {"escalation.yaml": text}), _settings())


def test_each_kind_parses_as_channel_or_a_list_of_users_and_groups(environment) -> None:
    text = SLACK.replace(
        "    gate:\n      - user_id: U0123456789",
        "    gate: channel\n    repair:\n      - user_id: U0123456789\n"
        "      - {group_id: S0123456789, organization_id: E0PARTNER01}",
    )
    (tenant_dir(environment) / "escalation.yaml").write_text(text)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert slack.approvers("gate") == Approvers(anyone_in_channel=True)
    assert slack.approvers("repair") == Approvers(
        users=(SlackUser("T0123456789", "U0123456789"),),
        groups=(SlackGroup("E0PARTNER01", "S0123456789"),),
    )


def test_a_group_entry_keeps_its_team_id_and_an_entry_without_one_keeps_none(environment) -> None:
    text = SLACK.replace(
        "- user_id: U0123456789",
        "- {group_id: S0123456789, team_id: T0WORKSPACE}\n      - group_id: S0999999999",
    )
    (tenant_dir(environment) / "escalation.yaml").write_text(text)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert slack.approvers("gate").groups == (
        SlackGroup("T0123456789", "S0123456789", "T0WORKSPACE"),
        SlackGroup("T0123456789", "S0999999999"),
    )


def _groups(count: int, start: int = 0) -> str:
    return "".join(f"      - group_id: S{index:010d}\n" for index in range(start, start + count))


@pytest.mark.parametrize("app", ["own", "shared"])
def test_more_distinct_groups_than_the_limit_across_kinds_are_refused(environment, app) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)
    base = SLACK if app == "own" else SHARED_SLACK
    allowlist = f"    gate:\n{_groups(6)}    repair:\n{_groups(6, start=5)}"
    text = base.replace("    gate:\n      - user_id: U0123456789\n", allowlist)
    (tenant_dir(environment) / "escalation.yaml").write_text(text)

    with pytest.raises(
        ConfigError,
        match=f"slack.allowlist names 11 distinct groups, above the limit of {MAX_APPROVER_GROUPS}",
    ):
        load_escalation_config(_settings())


def test_the_limit_counts_a_group_named_under_two_kinds_once(environment) -> None:
    allowlist = f"    gate:\n{_groups(MAX_APPROVER_GROUPS)}    repair:\n{_groups(1)}"
    text = SLACK.replace("    gate:\n      - user_id: U0123456789\n", allowlist)
    (tenant_dir(environment) / "escalation.yaml").write_text(text)

    slack = load_escalation_config(_settings()).slack

    assert slack is not None
    assert len(slack.approvers("gate").groups) == MAX_APPROVER_GROUPS
