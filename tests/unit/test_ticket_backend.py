import pytest
from scripts.config import Settings
from services.integration.credentials import ConfigError, EnvSecretResolver
from services.integration.jira import CLOUD, JiraAdapter
from services.integration.jira.credentials import (
    JiraCredentialResolver,
    build_single_project_resolver,
    parse_jira_config,
)
from services.integration.tickets import (
    JIRA,
    REBAR,
    TicketBackend,
    build_ticket_backend,
    build_ticket_backend_for_issue,
    resolve_ticket_backend_name,
)


@pytest.fixture(autouse=True)
def _no_acli_auth(monkeypatch):
    monkeypatch.setattr(
        "services.integration.jira.adapter.ensure_acli_authenticated_cached",
        lambda *args, **kwargs: False,
    )


def _resolver(mode: str = "cloud") -> JiraCredentialResolver:
    return build_single_project_resolver(
        url="https://jira.example",
        auth_mode=mode,
        email="agent@example",
        token="tok",
        board="AGENTDEV",
        project="AGENTDEV",
    )


def _routing_resolver() -> JiraCredentialResolver:
    raw = {
        "sites": {"cloud": {"url": "https://a.atlassian.net", "auth_mode": "cloud"}},
        "default": {"site": "cloud", "email": "agent@a.com", "token_ref": "TOK_A", "board": "AAA"},
    }
    return JiraCredentialResolver(
        parse_jira_config(raw), {"AAA"}, EnvSecretResolver({"TOK_A": "a"})
    )


def test_default_settings_resolve_jira() -> None:
    assert resolve_ticket_backend_name(Settings(_env_file=None)) == JIRA


def test_unset_backend_resolves_jira() -> None:
    assert resolve_ticket_backend_name(Settings(ticket_backend="")) == JIRA


def test_backend_name_is_case_insensitive() -> None:
    assert resolve_ticket_backend_name(Settings(ticket_backend="Jira")) == JIRA
    assert resolve_ticket_backend_name(Settings(ticket_backend="REBAR")) == REBAR


def test_unknown_backend_fails_closed() -> None:
    with pytest.raises(ConfigError, match="unknown ticket_backend"):
        resolve_ticket_backend_name(Settings(ticket_backend="asana"))


def test_factory_returns_jira_backend_by_default() -> None:
    backend = build_ticket_backend(
        Settings(_env_file=None), project="AGENTDEV", resolver=_resolver()
    )
    assert isinstance(backend, JiraAdapter)
    assert backend.source == CLOUD


def test_factory_for_issue_routes_by_project() -> None:
    backend = build_ticket_backend_for_issue(
        "AAA-1", Settings(_env_file=None), resolver=_routing_resolver()
    )
    assert isinstance(backend, JiraAdapter)
    assert backend.project == "AAA"


def test_jira_adapter_conforms_to_the_ticket_backend_protocol() -> None:
    backend = build_ticket_backend(
        Settings(_env_file=None), project="AGENTDEV", resolver=_resolver("cloud")
    )
    assert backend.source == CLOUD
    assert isinstance(backend, TicketBackend)
    for method in (
        "get_issue",
        "get_status",
        "transition",
        "add_comment",
        "list_comment_bodies",
        "search_issues",
        "create_issue",
        "render_comment",
    ):
        assert callable(getattr(backend, method))


def test_factory_dispatches_to_the_rebar_backend(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(
        "services.integration.tickets.rebar_backend.build_rebar_ticket_backend",
        lambda settings=None, *, project="": sentinel,
    )
    result = build_ticket_backend(Settings(ticket_backend="rebar"), project="ADSER")
    assert result is sentinel
