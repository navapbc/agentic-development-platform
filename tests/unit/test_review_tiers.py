"""Unit tests for the native Bedrock review-tier resolver and model-class config."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from services.model_gateway.review_tiers import (
    BEDROCK_PROVIDER,
    REVIEW_TIER_MODELS,
    UnknownReviewTierError,
    resolve_review_tier,
    review_primary_ref,
)
from services.review.llm import REVIEW_TIERS

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "platform" / "rebar-review.toml"


def test_every_review_tier_has_a_native_mapping():
    assert set(REVIEW_TIER_MODELS) == set(REVIEW_TIERS)


def test_resolve_review_tier_returns_the_bedrock_model_id():
    assert resolve_review_tier("review-model") == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_review_primary_ref_is_provider_qualified():
    assert review_primary_ref("review-model") == (
        f"{BEDROCK_PROVIDER}:us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )


def test_every_tier_is_a_claude_model():
    for tier, model in REVIEW_TIER_MODELS.items():
        assert "anthropic.claude" in model, f"{tier} resolves to {model}"


def test_the_two_opus_tiers_share_one_model():
    assert resolve_review_tier("security-review-model") == resolve_review_tier("evaluation-model")


def test_unknown_tier_is_rejected():
    with pytest.raises(UnknownReviewTierError):
        resolve_review_tier("frontier")


def _model_classes() -> dict:
    with _CONFIG.open("rb") as handle:
        return tomllib.load(handle)["llm"]["model_classes"]


def test_config_maps_review_primaries_to_rebar_classes():
    classes = _model_classes()

    assert classes["standard"]["model"] == resolve_review_tier("review-model")
    assert classes["standard"]["provider"] == BEDROCK_PROVIDER
    assert classes["frontier"]["model"] == resolve_review_tier("security-review-model")
    assert classes["frontier"]["provider"] == BEDROCK_PROVIDER


def test_no_rebar_class_declares_a_fallback_leg():
    """A fallback leg answers from a different model family, which the platform does not accept."""
    for name, slot in _model_classes().items():
        assert "fallback" not in slot, f"{name} declares a fallback leg"


def test_rebar_resolves_an_empty_fallback_chain_for_every_tier(monkeypatch: pytest.MonkeyPatch):
    from rebar.llm.model_classes import fallback_targets_for

    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(_CONFIG))
    for tier in REVIEW_TIERS:
        assert list(fallback_targets_for(review_primary_ref(tier))) == []


def test_rebar_retry_budget_is_declared_and_wider_than_the_library_default():
    from rebar.llm.config import DEFAULT_LLM_RETRY_MAX_ATTEMPTS

    with _CONFIG.open("rb") as handle:
        llm = tomllib.load(handle)["llm"]

    assert llm["llm_retry_max_attempts"] > DEFAULT_LLM_RETRY_MAX_ATTEMPTS
