"""Unit tests for settings and allowlist parsing."""

from __future__ import annotations

from scripts.config import Settings


def test_default_allowlists_match_plan():
    s = Settings()
    assert s.allowed_projects() == {"AGENTDEV"}
    assert s.allowed_repos() == {
        "agentic-demo-service",
        "agentic-demo-greenfield",
        "agentic-demo-ts",
        "agentic-demo-tf",
        "agentic-demo-java",
        "agentic-demo-php",
    }


def test_allowlist_parsing_trims_whitespace_and_drops_blanks():
    s = Settings(
        allowlist_jira_projects=" AGENTDEV , ",
        allowlist_repos="repo-a, repo-b ,",
    )
    assert s.allowed_projects() == {"AGENTDEV"}
    assert s.allowed_repos() == {"repo-a", "repo-b"}


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


def test_public_bedrock_endpoint_is_disabled_by_default():
    assert Settings().allow_public_bedrock_endpoint is False


def test_code_defaults_carry_no_project_specific_jumphost():
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
