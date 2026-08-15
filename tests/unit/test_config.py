"""Unit tests for settings and allowlist parsing."""

from __future__ import annotations

from pathlib import Path

from scripts.config import Settings
from services.integration.github.credentials import allowed_repos
from services.integration.jira.credentials import acceptance_field, allowed_projects


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
    settings = Settings(_env_file=None)

    assert allowed_repos(settings, environment="local", root=tmp_path) == {"repo-a", "repo-b"}
    assert allowed_projects(settings, environment="local", root=tmp_path) == {"AGENTDEV"}
    assert acceptance_field(settings, environment="local", root=tmp_path) == "customfield_10"


def test_allowlist_parsing_trims_whitespace_and_drops_blanks(tmp_path: Path) -> None:
    body = "config_version: 1\nallowlist:\n  - ' repo-a '\n  - ''\n  - repo-b\n"
    _write(tmp_path, "github", body)
    settings = Settings(_env_file=None)

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
    s = Settings()
    assert s.aws_region == "us-east-1"
    assert s.aws_profile == "mpse-legacy"


def test_bedrock_endpoint_is_standard_regional_host():
    assert Settings().bedrock_runtime_endpoint == "com.amazonaws.us-east-1.bedrock-runtime"


def test_default_connectivity_provider_is_jumphost():
    assert Settings().bedrock_connectivity_provider == "jumphost"


def test_code_defaults_carry_no_project_specific_jumphost(monkeypatch):
    monkeypatch.delenv("JUMPHOST_NAME", raising=False)
    monkeypatch.delenv("JUMPHOST_ENV", raising=False)
    s = Settings(_env_file=None)
    assert s.jumphost_name == ""
    assert s.jumphost_env == ""


def test_bedrock_client_kwargs_omits_endpoint_when_unset():
    kwargs = Settings(bedrock_endpoint_url="").bedrock_client_kwargs()
    assert kwargs == {"region_name": "us-east-1"}


def test_bedrock_client_kwargs_injects_endpoint_when_set():
    url = "https://bedrock-runtime.us-east-1.amazonaws.com"
    kwargs = Settings(bedrock_endpoint_url=url).bedrock_client_kwargs()
    assert kwargs == {"region_name": "us-east-1", "endpoint_url": url}
