"""Unit tests for the ticket source readiness check that pauses the intake schedule."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from services.integration.intake_readiness import acli_blocker, intake_blocker
from tests.support import hermetic_settings

TOKEN = "INTAKE_READINESS_TEST_TOKEN"


def _jira_config(monkeypatch: pytest.MonkeyPatch, directory: Path, auth_mode: str) -> None:
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
  email: agent@example.com
  token_ref: {TOKEN}
  board: ADP
projects: {{}}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(directory))


def _executable(path: Path, body: bytes) -> str:
    path.write_bytes(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


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


def test_a_cloud_site_also_needs_a_runnable_acli(monkeypatch, tmp_path) -> None:
    _jira_config(monkeypatch, tmp_path / "config", "cloud")
    monkeypatch.setenv(TOKEN, "token")
    missing = str(tmp_path / "missing-acli")
    acli = _executable(tmp_path / "acli", b"#!/bin/sh\nexit 0\n")

    blocked = intake_blocker("jira", hermetic_settings(jira_acli_path=missing))
    ready = intake_blocker("jira", hermetic_settings(jira_acli_path=acli))

    assert blocked == f"acli is not installed at {missing}, which the Jira Cloud path needs"
    assert ready == ""


@pytest.mark.skipif(os.name != "posix", reason="exec format errors are a POSIX behavior")
def test_an_acli_built_for_another_platform_blocks_intake(tmp_path) -> None:
    foreign = _executable(tmp_path / "acli", b"\xcf\xfa\xed\xfe" + b"\0" * 64)

    assert acli_blocker(foreign).startswith(f"acli at {foreign} cannot run on this platform")
