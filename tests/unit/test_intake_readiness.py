"""Unit tests for the ticket source readiness check that pauses the intake schedule."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from services.integration.intake_readiness import intake_blocker
from services.integration.jira.cloud_client import JiraCloudClient, JiraCloudError
from tests.support import hermetic_settings

TOKEN = "INTAKE_READINESS_TEST_TOKEN"


def _jira_config(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    auth_mode: str,
    email: str = "agent@example.com",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "jira.yaml").write_text(
        f"""config_version: 1
allowlist: [ADP]
acceptance_field: ""
sites:
  main:
    url: https://example.atlassian.net
    auth_mode: {auth_mode}
default:
  site: main
  email: "{email}"
  token_ref: {TOKEN}
  board: ADP
projects: {{}}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(directory))


def test_a_rebar_source_needs_no_secret() -> None:
    assert intake_blocker("rebar", hermetic_settings()) == ""


def test_a_jira_token_without_a_value_blocks_intake(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "datacenter")
    monkeypatch.delenv(TOKEN, raising=False)

    assert intake_blocker("jira", hermetic_settings()) == f"the Jira token {TOKEN} has no value"


def test_a_datacenter_site_with_its_token_is_ready(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "datacenter")
    monkeypatch.setenv(TOKEN, "token")

    assert intake_blocker("jira", hermetic_settings()) == ""


def test_a_cloud_site_whose_credential_authenticates_is_ready(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "cloud")
    monkeypatch.setenv(TOKEN, "token")
    monkeypatch.setattr(JiraCloudClient, "myself", lambda self: {"accountId": "a"})

    assert intake_blocker("jira", hermetic_settings()) == ""


def test_a_cloud_site_that_refuses_the_credential_blocks_intake(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "cloud")
    monkeypatch.setenv(TOKEN, "token")

    def _refuse(self):
        raise JiraCloudError("Jira Cloud answered 401", 401)

    monkeypatch.setattr(JiraCloudClient, "myself", _refuse)

    assert intake_blocker("jira", hermetic_settings()) == (
        "Jira Cloud at https://example.atlassian.net refused the credential with 401"
    )


def test_an_unreachable_cloud_site_does_not_block_intake(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "cloud")
    monkeypatch.setenv(TOKEN, "token")

    def _unreachable(self):
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(JiraCloudClient, "myself", _unreachable)

    assert intake_blocker("jira", hermetic_settings()) == ""


def test_a_cloud_site_without_an_email_blocks_intake(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "cloud", email="")
    monkeypatch.setenv(TOKEN, "token")

    assert intake_blocker("jira", hermetic_settings()) == (
        "jira.yaml names no allowlisted project with a site, token, and board"
    )
