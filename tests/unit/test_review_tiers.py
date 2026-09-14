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
    review_fallback_ref,
    review_primary_ref,
)
from services.review.llm import REVIEW_TIERS

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "local" / "rebar-review.toml"


def test_every_review_tier_has_a_native_mapping():
    assert set(REVIEW_TIER_MODELS) == set(REVIEW_TIERS)


def test_resolve_review_tier_returns_primary_and_fallback():
    primary, fallback = resolve_review_tier("review-model")
    assert primary == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert fallback == "us.amazon.nova-pro-v1:0"


def test_review_primary_ref_is_provider_qualified():
    assert review_primary_ref("review-model") == (
        f"{BEDROCK_PROVIDER}:us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )


def test_review_fallback_ref_is_provider_qualified_nova():
    assert review_fallback_ref("evaluation-model") == f"{BEDROCK_PROVIDER}:us.amazon.nova-pro-v1:0"


def test_shared_opus_tiers_share_one_nova_fallback():
    _, security_fallback = resolve_review_tier("security-review-model")
    eval_primary, eval_fallback = resolve_review_tier("evaluation-model")
    security_primary, _ = resolve_review_tier("security-review-model")

    assert security_primary == eval_primary
    assert security_fallback == eval_fallback


def test_unknown_tier_is_rejected():
    with pytest.raises(UnknownReviewTierError):
        resolve_review_tier("frontier")


def test_rebar_should_fall_back_is_availability_only():
    from pydantic_ai.exceptions import ModelHTTPError
    from rebar.llm.model_classes import should_fall_back

    availability = ModelHTTPError(status_code=503, model_name="x", body=None)
    assert should_fall_back(availability) is True
    assert should_fall_back(ValueError("structured output retry exhausted")) is False


def _model_classes() -> dict:
    with _CONFIG.open("rb") as handle:
        return tomllib.load(handle)["llm"]["model_classes"]


def test_config_maps_review_primaries_to_rebar_classes():
    classes = _model_classes()
    standard_primary, _ = resolve_review_tier("review-model")
    frontier_primary, _ = resolve_review_tier("security-review-model")

    assert classes["standard"]["model"] == standard_primary
    assert classes["standard"]["provider"] == BEDROCK_PROVIDER
    assert classes["frontier"]["model"] == frontier_primary
    assert classes["frontier"]["provider"] == BEDROCK_PROVIDER


def test_config_fallback_chains_match_the_tier_mapping():
    classes = _model_classes()
    _, standard_fallback = resolve_review_tier("review-model")
    _, frontier_fallback = resolve_review_tier("security-review-model")

    assert [f["model"] for f in classes["standard"]["fallback"]] == [standard_fallback]
    assert [f["model"] for f in classes["frontier"]["fallback"]] == [frontier_fallback]
    for slot in classes.values():
        for entry in slot["fallback"]:
            assert entry["provider"] == BEDROCK_PROVIDER


def test_config_resolves_through_rebar_with_the_pointer(monkeypatch: pytest.MonkeyPatch):
    from rebar.llm.model_classes import fallback_targets_for

    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(_CONFIG))
    for tier in REVIEW_TIERS:
        targets = fallback_targets_for(review_primary_ref(tier))
        _, fallback = resolve_review_tier(tier)
        assert [t.model for t in targets] == [fallback]
        assert all(t.provider == BEDROCK_PROVIDER for t in targets)
