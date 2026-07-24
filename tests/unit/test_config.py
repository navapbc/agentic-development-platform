"""Unit tests for settings and allowlist parsing."""

from __future__ import annotations

from scripts.config import Settings


def test_default_allowlists_match_plan():
    s = Settings()
    assert s.allowed_projects() == {"AGENTDEV"}
    assert s.allowed_repos() == {"agentic-demo-service", "agentic-demo-greenfield"}


def test_allowlist_parsing_trims_whitespace_and_drops_blanks():
    s = Settings(
        allowlist_jira_projects=" AGENTDEV , ",
        allowlist_repos="repo-a, repo-b ,",
    )
    assert s.allowed_projects() == {"AGENTDEV"}
    assert s.allowed_repos() == {"repo-a", "repo-b"}


def test_region_and_profile_defaults_are_fedramp_aligned():
    s = Settings()
    assert s.aws_region == "us-east-1"
    assert s.aws_profile == "mpse-legacy"


def test_bedrock_endpoint_is_standard_regional_host():
    assert Settings().bedrock_runtime_endpoint == "com.amazonaws.us-east-1.bedrock-runtime"
