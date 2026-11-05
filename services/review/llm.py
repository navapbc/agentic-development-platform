"""rebar LLM configuration for the review gates.

The proxied path routes through the LiteLLM gateway. The native path points rebar
at Bedrock directly, with the model set to the tier's provider-qualified primary so
rebar builds its own model object. That object carries a ``BedrockModelProfile``,
which is what enables Anthropic prompt caching. The model-class config
(``REBAR_LLM_CONFIG_FILE``) declares no fallback leg, so a tier that cannot reach
Claude fails once rebar exhausts its retry budget rather than answering from a
different model family.
"""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from scripts.config import Settings, get_settings

from services.model_gateway.llm_config import llm_config_for
from services.model_gateway.review_tiers import review_primary_ref

REVIEW_TIERS = ("review-model", "security-review-model", "evaluation-model")


def review_llm_config(tier: str, settings: Settings | None = None) -> LLMConfig:
    resolved = settings or get_settings()
    if not resolved.rebar_review_native:
        return llm_config_for(tier, resolved)
    return LLMConfig(
        runner="pydantic_ai",
        model=review_primary_ref(tier),
        model_provider="bedrock",
        bedrock_region_name=resolved.aws_region,
        max_tokens=resolved.rebar_llm_max_tokens,
        temperature=resolved.rebar_llm_temperature,
    )
