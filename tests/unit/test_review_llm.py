"""Unit tests for the review-gate rebar LLM configuration."""

from __future__ import annotations

from rebar.llm.providers import ProviderSession
from scripts.config import Settings
from services.review.llm import REVIEW_TIERS, review_llm_config


def _settings() -> Settings:
    return Settings(
        rebar_review_native=False,
        rebar_llm_base_url="http://litellm:4000",
        rebar_llm_api_key="gateway-key",
        rebar_llm_model_provider="openai-chat",
        rebar_llm_max_tokens=8192,
    )


def test_review_llm_config_targets_the_litellm_gateway():
    cfg = review_llm_config("review-model", _settings())

    assert cfg.runner == "pydantic_ai"
    assert cfg.base_url == "http://litellm:4000"
    assert cfg.api_key == "gateway-key"
    assert cfg.model_provider == "openai-chat"
    assert cfg.model == "review-model"
    assert cfg.max_tokens == 8192


def test_review_llm_config_builds_an_openai_compatible_model_per_tier():
    settings = _settings()

    for tier in REVIEW_TIERS:
        cfg = review_llm_config(tier, settings)
        with ProviderSession(cfg) as session:
            model = session.model_for(f"{cfg.model_provider}:{cfg.model}")

            assert model.model_name == tier
            assert str(model.base_url) == "http://litellm:4000"


def test_review_llm_config_defaults_to_native_bedrock():
    from services.model_gateway.review_tiers import review_primary_ref

    settings = Settings(
        aws_region="us-west-2",
        rebar_llm_max_tokens=8192,
    )

    cfg = review_llm_config("review-model", settings)

    assert cfg.model_provider == "bedrock"
    assert cfg.bedrock_region_name == "us-west-2"
    assert cfg.base_url is None
    assert cfg.model == review_primary_ref("review-model")


def test_review_llm_config_uses_the_proxied_path_when_the_flag_is_cleared():
    cfg = review_llm_config("review-model", _settings())

    assert cfg.model_provider == "openai-chat"
    assert cfg.base_url == "http://litellm:4000"
    assert cfg.bedrock_region_name is None


def test_review_llm_config_uses_native_bedrock_when_the_flag_is_set():
    from services.model_gateway.review_tiers import review_primary_ref

    settings = Settings(
        rebar_review_native=True,
        aws_region="us-west-2",
        rebar_llm_max_tokens=8192,
    )

    for tier in REVIEW_TIERS:
        cfg = review_llm_config(tier, settings)

        assert cfg.runner == "pydantic_ai"
        assert cfg.model == review_primary_ref(tier)
        assert cfg.model.startswith("bedrock:")
        assert cfg.model_provider == "bedrock"
        assert cfg.bedrock_region_name == "us-west-2"
        assert cfg.base_url is None
        assert cfg.api_key is None
        assert cfg.max_tokens == 8192
