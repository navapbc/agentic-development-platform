"""Unit tests for settings and allowlist parsing."""

from __future__ import annotations

from pathlib import Path

from scripts.config import Settings, to_host_hostport, to_host_url
from services.integration.github.credentials import allowed_repos
from services.integration.jira.credentials import acceptance_field, allowed_projects
from tests.support import hermetic_settings


def _write(root: Path, name: str, body: str) -> None:
    directory = root / "config" / "local"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(body, encoding="utf-8")


def test_allowlists_are_read_from_the_config_files(tmp_path: Path) -> None:
    _write(tmp_path, "github", "config_version: 1\nallowlist:\n  - repo-a\n  - repo-b\n")
    _write(
        tmp_path,
        "jira",
        "config_version: 1\nallowlist:\n  - AGENTDEV\nacceptance_field: customfield_10\n",
    )
    settings = hermetic_settings()

    assert allowed_repos(settings, environment="local", root=tmp_path) == {"repo-a", "repo-b"}
    assert allowed_projects(settings, environment="local", root=tmp_path) == {"AGENTDEV"}
    assert acceptance_field(settings, environment="local", root=tmp_path) == "customfield_10"


def test_allowlist_parsing_trims_whitespace_and_drops_blanks(tmp_path: Path) -> None:
    body = "config_version: 1\nallowlist:\n  - ' repo-a '\n  - ''\n  - repo-b\n"
    _write(tmp_path, "github", body)
    settings = hermetic_settings()

    assert allowed_repos(settings, environment="local", root=tmp_path) == {"repo-a", "repo-b"}


def test_repo_intelligence_registry_parses_entries():
    s = Settings(repo_intelligence_repos="svc=/repos/svc@abc123, green=/repos/green")
    assert s.repo_intelligence_registry() == {
        "svc": ("/repos/svc", "abc123"),
        "green": ("/repos/green", None),
    }


def test_repo_intelligence_registry_falls_back_to_single_root():
    s = Settings(
        repo_intelligence_repo_root="/repos/agentic-demo-service",
        repo_intelligence_commit="0744a6e",
    )
    assert s.repo_intelligence_registry() == {
        "agentic-demo-service": ("/repos/agentic-demo-service", "0744a6e")
    }


def test_region_and_profile_defaults_are_fedramp_aligned():
    s = hermetic_settings()
    assert s.aws_region == "us-east-1"
    assert s.aws_profile == "agentic-platform"


def test_bedrock_endpoint_is_standard_regional_host():
    assert hermetic_settings().bedrock_runtime_endpoint == "com.amazonaws.us-east-1.bedrock-runtime"


def test_default_connectivity_provider_is_jumphost():
    assert hermetic_settings().bedrock_connectivity_provider == "jumphost"


def test_code_defaults_carry_no_project_specific_jumphost(monkeypatch):
    monkeypatch.delenv("JUMPHOST_NAME", raising=False)
    monkeypatch.delenv("JUMPHOST_ENV", raising=False)
    s = hermetic_settings()
    assert s.jumphost_name == ""
    assert s.jumphost_env == ""


def _classification_days(monkeypatch, value: str) -> Settings:
    monkeypatch.setenv("EVIDENCE_RETENTION_CLASSIFICATION_DAYS", value)
    return hermetic_settings()


def test_classification_retention_parses_pairs(monkeypatch):
    s = _classification_days(monkeypatch, "security:3650, audit:3650")
    assert s.evidence_retention_classification_days == {"security": 3650, "audit": 3650}


def test_classification_retention_empty_is_blank_mapping(monkeypatch):
    s = _classification_days(monkeypatch, "")
    assert s.evidence_retention_classification_days == {}


def test_classification_retention_rejects_unknown_key(monkeypatch):
    import pytest

    with pytest.raises(ValueError):
        _classification_days(monkeypatch, "bogus:10")


def test_classification_retention_rejects_non_numeric_days(monkeypatch):
    import pytest

    with pytest.raises(ValueError):
        _classification_days(monkeypatch, "security:soon")


def test_host_rewrite_maps_compose_service_names_to_localhost(monkeypatch):
    monkeypatch.delenv("PLATFORM_IN_CONTAINER", raising=False)

    assert (
        to_host_url("postgresql+psycopg://platform:pw@postgres:5432/agent_platform")
        == "postgresql+psycopg://platform:pw@localhost:5432/agent_platform"
    )
    assert to_host_hostport("temporal:7233") == "localhost:7233"


def test_host_rewrite_leaves_unknown_hosts_alone(monkeypatch):
    monkeypatch.delenv("PLATFORM_IN_CONTAINER", raising=False)

    assert to_host_url("https://api.github.com/x") == "https://api.github.com/x"
    assert to_host_hostport("temporal.example.com:7233") == "temporal.example.com:7233"


def test_host_rewrite_is_inert_inside_the_platform_image(monkeypatch):
    """Compose service names resolve inside the image, where localhost is the container."""
    monkeypatch.setenv("PLATFORM_IN_CONTAINER", "1")

    dsn = "postgresql+psycopg://platform:pw@postgres:5432/agent_platform"
    assert to_host_url(dsn) == dsn
    assert to_host_hostport("temporal:7233") == "temporal:7233"
