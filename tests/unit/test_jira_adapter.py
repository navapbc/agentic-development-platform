import sys

import pytest
from scripts.config import Settings
from services.integration.jira import (
    CLOUD,
    DATACENTER,
    JiraAdapter,
    build_jira_adapter,
)

_GIT_MARKERS = ("apply_outbound", "git_adapter", "_ref_lock", "_advisory_lock")


def _settings(mode: str) -> Settings:
    return Settings(
        jira_auth_mode=mode,
        jira_base_url="https://jira.example",
        jira_project="AGENTDEV",
        jira_email="agent@example",
        jira_api_token="",
        jira_pat="",
        jira_acli_path="/usr/local/bin/acli",
    )


def test_build_datacenter_adapter():
    adapter = build_jira_adapter(_settings("datacenter"))
    assert isinstance(adapter, JiraAdapter)
    assert adapter.source == DATACENTER
    assert type(adapter.client).__name__ == "JiraDataCenterClient"


def test_build_cloud_adapter():
    adapter = build_jira_adapter(_settings("cloud"))
    assert adapter.source == CLOUD
    assert type(adapter.client).__name__ == "AcliClient"


def test_auth_mode_is_case_insensitive():
    assert build_jira_adapter(_settings("DataCenter")).source == DATACENTER


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_jira_adapter(_settings("bogus"))


def test_construction_stays_git_free():
    build_jira_adapter(_settings("datacenter"))
    build_jira_adapter(_settings("cloud"))
    loaded = sorted(m for m in sys.modules if any(marker in m for marker in _GIT_MARKERS))
    assert loaded == []
