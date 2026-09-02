"""rebar LLM configuration for the review gates, pointed at the LiteLLM gateway."""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from scripts.config import Settings

from services.model_gateway.llm_config import llm_config_for

REVIEW_TIERS = ("review-model", "security-review-model", "evaluation-model")


def review_llm_config(tier: str, settings: Settings | None = None) -> LLMConfig:
    return llm_config_for(tier, settings)
