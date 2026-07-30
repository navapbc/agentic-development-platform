"""Unit tests for the review-gate rebar LLM configuration."""

from __future__ import annotations

from rebar.llm.openai_model import build_openai_compatible_model
from scripts.config import Settings
from services.review.llm import REVIEW_TIERS, review_llm_config


def _settings() -> Settings:
    return Settings(
        rebar_llm_base_url="http://litellm:4000",
        rebar_llm_api_key="gateway-key",
        rebar_llm_model_provider="openai",
        rebar_llm_max_tokens=8192,
    )


def test_review_llm_config_targets_the_litellm_gateway():
    cfg = review_llm_config("review-model", _settings())

    assert cfg.runner == "pydantic_ai"
    assert cfg.base_url == "http://litellm:4000"
    assert cfg.api_key == "gateway-key"
    assert cfg.model_provider == "openai"
    assert cfg.model == "review-model"
    assert cfg.max_tokens == 8192


def test_review_llm_config_builds_an_openai_compatible_model_per_tier():
    settings = _settings()

    for tier in REVIEW_TIERS:
        cfg = review_llm_config(tier, settings)
        model = build_openai_compatible_model(cfg.model, cfg=cfg)

        assert model.model_name == tier
        assert model.base_url == "http://litellm:4000"
