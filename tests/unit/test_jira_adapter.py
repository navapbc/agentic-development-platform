import sys

import pytest
from services.integration.credentials import ConfigError, EnvSecretResolver
from services.integration.jira import (
    CLOUD,
    DATACENTER,
    JiraAdapter,
    build_jira_adapter,
    build_jira_adapter_for_issue,
)
from services.integration.jira.credentials import (
    JiraCredentialResolver,
    build_single_project_resolver,
    parse_jira_config,
)

_GIT_MARKERS = ("apply_outbound", "git_adapter", "_ref_lock", "_advisory_lock")


@pytest.fixture
def fake_dc_jira(monkeypatch):
    from services.integration.jira._engine import ensure_engine_on_path

    ensure_engine_on_path()
    from rebar_reconciler.adapters.jira_datacenter import transport

    class _FakeJira:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(transport, "_jira_client_class", lambda: _FakeJira)
    return _FakeJira


def _resolver(mode: str) -> JiraCredentialResolver:
    return build_single_project_resolver(
        url="https://jira.example",
        auth_mode=mode,
        email="agent@example",
        token="tok",
        board="AGENTDEV",
        project="AGENTDEV",
    )


def test_build_datacenter_adapter(fake_dc_jira):
    adapter = build_jira_adapter(project="AGENTDEV", resolver=_resolver("datacenter"))
    assert isinstance(adapter, JiraAdapter)
    assert adapter.source == DATACENTER
    assert type(adapter.client).__name__ == "JiraDataCenterTransport"


def test_build_cloud_adapter():
    adapter = build_jira_adapter(project="AGENTDEV", resolver=_resolver("cloud"))
    assert adapter.source == CLOUD
    assert type(adapter.client).__name__ == "AcliClient"


def test_auth_mode_is_case_insensitive(fake_dc_jira):
    adapter = build_jira_adapter(project="AGENTDEV", resolver=_resolver("DataCenter"))
    assert adapter.source == DATACENTER


def test_unknown_mode_fails_closed():
    with pytest.raises(ConfigError):
        _resolver("bogus")


def test_build_jira_adapter_for_issue_routes_by_project(fake_dc_jira):
    raw = {
        "sites": {
            "cloud": {"url": "https://a.atlassian.net", "auth_mode": "cloud"},
            "dc": {"url": "https://b.internal", "auth_mode": "datacenter"},
        },
        "default": {"site": "cloud", "email": "agent@a.com", "token_ref": "TOK_A", "board": "AAA"},
        "projects": {"BBB": {"site": "dc", "token_ref": "TOK_B", "board": "BBB"}},
    }
    resolver = JiraCredentialResolver(
        parse_jira_config(raw), {"AAA", "BBB"}, EnvSecretResolver({"TOK_A": "a", "TOK_B": "b"})
    )

    a = build_jira_adapter_for_issue("AAA-1", resolver=resolver)
    b = build_jira_adapter_for_issue("BBB-2", resolver=resolver)

    assert a.source == CLOUD
    assert a.project == "AAA"
    assert type(a.client).__name__ == "AcliClient"
    assert b.source == DATACENTER
    assert b.project == "BBB"
    assert type(b.client).__name__ == "JiraDataCenterTransport"


def test_construction_stays_git_free(fake_dc_jira):
    build_jira_adapter(project="AGENTDEV", resolver=_resolver("datacenter"))
    build_jira_adapter(project="AGENTDEV", resolver=_resolver("cloud"))
    loaded = sorted(m for m in sys.modules if any(marker in m for marker in _GIT_MARKERS))
    assert loaded == []
