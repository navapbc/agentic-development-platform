"""Unit tests for the Jira credential resolver: named sites, default identity, overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretNotFoundError,
)
from services.integration.jira.credentials import (
    JiraCredentialResolver,
    JiraProjectCredential,
    UnconfiguredProjectError,
    parse_jira_config,
)


def _config() -> dict:
    return {
        "sites": {
            "cloud": {"url": "https://acme.atlassian.net", "auth_mode": "cloud"},
            "dc": {"url": "https://jira.acme.internal", "auth_mode": "datacenter"},
        },
        "default": {
            "site": "cloud",
            "email": "agent@acme.com",
            "token_ref": "TOKEN_CLOUD",
            "board": "AGENTDEV",
        },
    }


def _resolver(raw: dict, allowlist: set[str], secrets: dict[str, str]) -> JiraCredentialResolver:
    return JiraCredentialResolver(parse_jira_config(raw), allowlist, EnvSecretResolver(secrets))


def test_default_resolves_any_allowlisted_project_without_enumeration():
    resolver = _resolver(_config(), {"PROJ-A", "PROJ-B"}, {"TOKEN_CLOUD": "ccc"})

    for project in ("PROJ-A", "PROJ-B"):
        assert resolver.resolve(project) == JiraProjectCredential(
            url="https://acme.atlassian.net",
            auth_mode="cloud",
            email="agent@acme.com",
            token="ccc",
            board="AGENTDEV",
            project=project,
        )
    assert resolver.projects() == {"PROJ-A", "PROJ-B"}


def test_non_allowlisted_project_resolves_none():
    resolver = _resolver(_config(), {"PROJ-A"}, {"TOKEN_CLOUD": "ccc"})

    assert resolver.resolve("PROJ-B") is None
    assert resolver.projects() == {"PROJ-A"}


def test_per_project_board_override_on_the_same_site_and_account():
    raw = _config()
    raw["projects"] = {"OPS": {"board": "OPS"}}
    resolver = _resolver(raw, {"CORE", "OPS"}, {"TOKEN_CLOUD": "ccc"})

    core = resolver.resolve("CORE")
    ops = resolver.resolve("OPS")

    assert core is not None and ops is not None
    assert core.url == ops.url == "https://acme.atlassian.net"
    assert core.token == ops.token == "ccc"
    assert core.board == "AGENTDEV"
    assert ops.board == "OPS"


def test_datacenter_project_coexists_with_a_cloud_default():
    raw = _config()
    raw["projects"] = {"PLATFORM": {"site": "dc", "token_ref": "TOKEN_PAT", "board": "PLATFORM"}}
    resolver = _resolver(raw, {"CORE", "PLATFORM"}, {"TOKEN_CLOUD": "ccc", "TOKEN_PAT": "ppp"})

    core = resolver.resolve("CORE")
    platform = resolver.resolve("PLATFORM")

    assert core == JiraProjectCredential(
        url="https://acme.atlassian.net",
        auth_mode="cloud",
        email="agent@acme.com",
        token="ccc",
        board="AGENTDEV",
        project="CORE",
    )
    assert platform == JiraProjectCredential(
        url="https://jira.acme.internal",
        auth_mode="datacenter",
        email="",
        token="ppp",
        board="PLATFORM",
        project="PLATFORM",
    )


def test_cloud_project_without_an_email_fails_closed():
    raw = _config()
    del raw["default"]["email"]
    resolver = _resolver(raw, {"CORE"}, {"TOKEN_CLOUD": "ccc"})

    with pytest.raises(UnconfiguredProjectError):
        resolver.resolve("CORE")


def test_no_default_and_incomplete_project_fails_closed():
    raw = _config()
    del raw["default"]
    raw["projects"] = {"ORPHAN": {"token_ref": "TOKEN_CLOUD"}}
    resolver = _resolver(raw, {"ORPHAN"}, {"TOKEN_CLOUD": "ccc"})

    with pytest.raises(UnconfiguredProjectError):
        resolver.resolve("ORPHAN")


def test_missing_secret_reference_fails_closed():
    resolver = _resolver(_config(), {"CORE"}, {})

    with pytest.raises(SecretNotFoundError):
        resolver.resolve("CORE")


def test_config_and_secrets_manager_seam_return_the_same_shape():
    class FakeSecretsManager:
        def __init__(self, values: dict[str, str]) -> None:
            self._values = values

        def resolve(self, ref: str) -> str:
            return self._values[ref]

    config = parse_jira_config(_config())
    from_env = JiraCredentialResolver(
        config, {"CORE"}, EnvSecretResolver({"TOKEN_CLOUD": "shared"})
    )
    from_sm = JiraCredentialResolver(
        config, {"CORE"}, FakeSecretsManager({"TOKEN_CLOUD": "shared"})
    )

    assert from_env.resolve("CORE") == from_sm.resolve("CORE")


def test_parse_rejects_default_referencing_unknown_site():
    raw = _config()
    raw["default"]["site"] = "nope"
    with pytest.raises(ConfigError):
        parse_jira_config(raw)


def test_parse_rejects_project_referencing_unknown_site():
    raw = _config()
    raw["projects"] = {"CORE": {"site": "nope"}}
    with pytest.raises(ConfigError):
        parse_jira_config(raw)


def test_parse_rejects_site_missing_fields():
    raw = {"sites": {"cloud": {"url": "https://acme.atlassian.net"}}}
    with pytest.raises(ConfigError):
        parse_jira_config(raw)


def test_parse_rejects_unsupported_auth_mode():
    raw = {"sites": {"cloud": {"url": "https://acme.atlassian.net", "auth_mode": "oauth"}}}
    with pytest.raises(ConfigError):
        parse_jira_config(raw)


def test_parse_accepts_empty_config():
    config = parse_jira_config({})
    assert config.sites == {}
    assert config.default is None
    assert config.projects == {}


def test_committed_example_template_is_valid_and_resolves():
    example = Path(__file__).resolve().parents[2] / "config" / "local" / "jira.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    resolver = JiraCredentialResolver(
        parse_jira_config(raw), {"AGENTDEV"}, EnvSecretResolver({"JIRA_API_TOKEN": "tok"})
    )

    cred = resolver.resolve("AGENTDEV")

    assert cred is not None
    assert cred.auth_mode == "cloud"
    assert cred.token == "tok"
    assert cred.board == "AGENTDEV"
    assert resolver.resolve("not-an-allowlisted-project") is None
