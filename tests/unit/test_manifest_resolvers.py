"""Unit tests for the manifest input resolvers."""

from __future__ import annotations

import httpx
import pytest
from services.orchestration.temporal_activities.bedrock import resolve_logical_models
from services.orchestration.temporal_activities.review import review_prompt_versions

pytestmark = pytest.mark.asyncio


def test_prompt_versions_map_enabled_review_gates():
    versions = review_prompt_versions(
        ["code-review", "security-review", "acceptance", "semgrep", "trivy"]
    )

    assert versions == {
        "code-review": "code-review/v1",
        "security-review": "security-review/v1",
        "acceptance": "acceptance-verification/v1",
    }


def test_prompt_versions_ignore_unknown_gate_names():
    assert review_prompt_versions(["semgrep", "trivy"]) == {}


class _Settings:
    litellm_base_url = "http://litellm:4000"
    litellm_master_key = "test-key"


async def test_resolve_logical_models_maps_logical_to_concrete():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/model/info"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "model_name": "planning-model",
                        "litellm_params": {"model": "bedrock/claude-plan"},
                    },
                    {
                        "model_name": "coding-model",
                        "litellm_params": {"model": "bedrock/claude-code"},
                    },
                    {
                        "model_name": "unused-model",
                        "litellm_params": {"model": "bedrock/other"},
                    },
                ]
            },
        )

    resolved = await resolve_logical_models(
        ["planning-model", "coding-model"],
        settings=_Settings(),
        transport=httpx.MockTransport(handler),
    )

    assert resolved == {
        "planning-model": "bedrock/claude-plan",
        "coding-model": "bedrock/claude-code",
    }


async def test_resolve_logical_models_skips_unmapped_names():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    resolved = await resolve_logical_models(
        ["planning-model"], settings=_Settings(), transport=httpx.MockTransport(handler)
    )

    assert resolved == {}


def test_litellm_auth_headers_are_shared_across_planner_calls():
    from dataclasses import dataclass

    from services.orchestration.temporal_activities.bedrock import _litellm_auth_headers

    @dataclass
    class _Settings:
        litellm_master_key: str

    headers = _litellm_auth_headers(_Settings(litellm_master_key="k-123"))
    assert headers == {"Authorization": "Bearer k-123"}
