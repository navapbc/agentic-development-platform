"""Live review-path checks against a running LiteLLM gateway and native Bedrock.

Skipped unless ``RUN_LIVE_LLM=1``; requires the local ``litellm`` service, the
``bedrock-tunnel`` sidecar, and AWS credentials scoped for Bedrock review calls.
"""

from __future__ import annotations

import os

import pytest
from rebar.llm.runner import RunRequest, get_runner
from scripts.config import Settings
from services.review.kernel import RebarReviewKernel
from services.review.llm import review_llm_config
from services.review.types import ReviewInput, ReviewStage, Verdict

_run_live = os.environ.get("RUN_LIVE_LLM") == "1"
requires_live_llm = pytest.mark.skipif(
    not _run_live, reason="set RUN_LIVE_LLM=1 to run the live review LLM tests"
)

_SOUND_PLAN = (
    "Plan: add a function add(a, b) that returns a + b, with a unit test asserting "
    "add(2, 3) == 5. No other changes."
)


def _proxied_settings() -> Settings:
    return Settings(
        rebar_review_native=False,
        rebar_llm_base_url="http://litellm:4000",
        rebar_llm_api_key="change-me-locally",
        rebar_llm_model_provider="openai",
        rebar_llm_max_tokens=8192,
    )


def _review_input() -> ReviewInput:
    return ReviewInput(
        stage=ReviewStage.PLAN_REVIEW,
        subject_ref="live-fixture",
        context_ref="live-fixture",
        prompt_name="plan-review",
        prompt_version="v1",
        system_prompt="You are a plan reviewer.",
        instructions=_SOUND_PLAN,
        reviewers=("plan-reviewer",),
    )


@requires_live_llm
def test_proxied_review_tier_routes_through_litellm():
    cfg = review_llm_config("review-model", _proxied_settings())
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


@requires_live_llm
def test_native_review_records_in_process_usage_and_the_answering_leg():
    cfg = review_llm_config("review-model")  # native default
    assert cfg.model_provider == "bedrock"

    raw = RebarReviewKernel()._run_multipass(_review_input(), cfg, "review-model")

    assert raw.verdict in (Verdict.PASS, Verdict.BLOCK)
    assert raw.usage.llm_calls > 0
    assert raw.usage.prompt_tokens > 0
    assert raw.usage.completion_tokens > 0
    assert raw.provenance
    assert raw.provenance[0].model_id is not None
    assert raw.provenance[0].model_id.startswith("bedrock:")


@requires_live_llm
def test_native_and_proxied_agree_on_the_verdict_over_a_shared_fixture():
    native = RebarReviewKernel()._run_multipass(
        _review_input(), review_llm_config("review-model"), "review-model"
    )
    proxied = RebarReviewKernel()._run_multipass(
        _review_input(),
        review_llm_config("review-model", _proxied_settings()),
        "review-model",
    )

    assert native.verdict == proxied.verdict
    assert native.usage.llm_calls > 0
    assert native.usage.prompt_tokens > 0
