"""Live review-tier check against a running LiteLLM gateway.

Skipped unless ``RUN_LIVE_LLM=1``; requires the local ``litellm`` service and AWS
credentials for Bedrock.
"""

from __future__ import annotations

import os

import pytest
from rebar.llm.runner import RunRequest, get_runner
from services.review.llm import review_llm_config

_run_live = os.environ.get("RUN_LIVE_LLM") == "1"
requires_live_llm = pytest.mark.skipif(
    not _run_live, reason="set RUN_LIVE_LLM=1 to run the live LiteLLM review test"
)


@requires_live_llm
def test_review_model_tier_routes_through_litellm():
    cfg = review_llm_config("review-model")
    runner = get_runner(cfg)

    result = runner.run(
        RunRequest(
            system_prompt="You are a code reviewer.",
            instructions="The change is empty. Return no findings.",
            config=cfg,
            reviewers=["reviewer-a"],
        )
    )

    assert result["runner"] == "pydantic_ai"
    assert result["model"]
