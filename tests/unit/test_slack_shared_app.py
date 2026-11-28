"""The shared Slack app's public settings come from the platform file slack-shared-app.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.integration.credentials import ConfigError
from services.integration.slack.shared_app import SharedSlackApp, load_shared_app
from tests.support import SHARED_APP_YAML

REPOSITORY = Path(__file__).resolve().parents[2]
SHARED_APP = SHARED_APP_YAML


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Path:
    env = tmp_path / "env"
    env.mkdir()
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    return env


def test_an_environment_without_the_file_has_no_shared_app(environment) -> None:
    assert load_shared_app() is None


def test_the_file_names_the_app_organization_and_api_base_url(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP)

    assert load_shared_app() == SharedSlackApp(
        app_id="A0SHARED001",
        organization_id="E0SHARED001",
        api_base_url="https://slack.com/api/",
    )


@pytest.mark.parametrize("key", ["app_id", "organization_id", "api_base_url"])
def test_a_file_without_a_setting_is_refused_with_the_key_named(environment, key) -> None:
    text = "".join(line + "\n" for line in SHARED_APP.splitlines() if not line.startswith(key))
    (environment / "slack-shared-app.yaml").write_text(text)

    with pytest.raises(ConfigError, match=f"slack-shared-app.yaml: {key} is required"):
        load_shared_app()


def test_a_file_older_than_version_1_is_refused(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(SHARED_APP.replace("config_version: 1", ""))

    with pytest.raises(ConfigError, match="is version 0 but this release expects version 1"):
        load_shared_app()


def test_the_shipped_example_loads(environment) -> None:
    (environment / "slack-shared-app.yaml").write_text(
        (REPOSITORY / "config/local/slack-shared-app.yaml.example").read_text()
    )

    assert load_shared_app() is not None


@pytest.mark.parametrize(
    ("text", "message"),
    [("app_id: [unclosed\n", "is not valid YAML"), (b"\xff\xfe", "could not be read")],
    ids=["invalid-yaml", "undecodable"],
)
def test_an_unparseable_file_is_a_config_error_naming_the_path(
    environment, text: str | bytes, message: str
) -> None:
    path = environment / "slack-shared-app.yaml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)

    with pytest.raises(ConfigError, match=message) as refused:
        load_shared_app()

    assert str(path) in str(refused.value)


def test_a_govslack_api_base_url_is_accepted(environment) -> None:
    url = "https://slack-gov.com/api/"
    (environment / "slack-shared-app.yaml").write_text(
        SHARED_APP.replace("https://slack.com/api/", url)
    )

    shared_app = load_shared_app()

    assert shared_app is not None and shared_app.api_base_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://slack.com/api/",
        "https://slack.example.com/api/",
        "https://slack.com:8443/api/",
        "https://user@slack.com/api/",
    ],
)
def test_an_api_base_url_off_the_slack_api_hosts_is_refused(environment, url: str) -> None:
    (environment / "slack-shared-app.yaml").write_text(
        SHARED_APP.replace("https://slack.com/api/", url)
    )

    with pytest.raises(ConfigError, match="is not an HTTPS URL on slack.com or slack-gov.com"):
        load_shared_app()
