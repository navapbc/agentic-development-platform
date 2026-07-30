"""rebar LLM configuration for the review gates, pointed at the LiteLLM gateway."""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from scripts.config import Settings, get_settings

REVIEW_TIERS = ("review-model", "security-review-model", "evaluation-model")


def review_llm_config(tier: str, settings: Settings | None = None) -> LLMConfig:
    settings = settings or get_settings()
    return LLMConfig(
        runner="pydantic_ai",
        model=tier,
        model_provider=settings.rebar_llm_model_provider,
        base_url=settings.rebar_llm_base_url,
        api_key=settings.rebar_llm_api_key,
        max_tokens=settings.rebar_llm_max_tokens,
    )
