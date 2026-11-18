"""Unit tests for settings and the Jira allowlist."""

from __future__ import annotations

from pathlib import Path

from scripts.config import Settings, to_host_hostport, to_host_url
from services.integration.jira.credentials import acceptance_field, allowed_projects
from tests.support import hermetic_settings


def _write(root: Path, name: str, body: str) -> None:
    directory = root / "config" / "local"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(body, encoding="utf-8")


def test_jira_allowlist_and_acceptance_field_are_read_from_the_config(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "jira",
        "config_version: 1\nallowlist:\n  - ' AGENTDEV '\n  - ''\n"
        "acceptance_field: customfield_10\n",
    )
    settings = hermetic_settings()

    assert allowed_projects(settings, environment="local", root=tmp_path) == {"AGENTDEV"}
    assert acceptance_field(settings, environment="local", root=tmp_path) == "customfield_10"


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


def _declared_ports() -> Settings:
    """Settings carrying the declared host ports, so a remapped .env cannot move the assertion."""
    return hermetic_settings(postgres_host_port=5432, temporal_host_port=7233)


def test_host_rewrite_maps_compose_service_names_to_localhost(monkeypatch):
    monkeypatch.delenv("PLATFORM_IN_CONTAINER", raising=False)

    ports = _declared_ports()

    assert (
        to_host_url("postgresql+psycopg://platform:pw@postgres:5432/agent_platform", ports)
        == "postgresql+psycopg://platform:pw@localhost:5432/agent_platform"
    )
    assert to_host_hostport("temporal:7233", ports) == "localhost:7233"


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


def test_the_local_artifacts_default_sits_under_the_shared_workspace_root():
    settings = hermetic_settings(execution_workspace_root="/workspaces/")

    assert settings.local_artifacts_root() == "/workspaces/.agent-publish"


def test_a_configured_local_artifacts_root_is_used_as_given():
    settings = hermetic_settings(source_control_local_artifacts_root="/srv/artifacts")

    assert settings.local_artifacts_root() == "/srv/artifacts"
