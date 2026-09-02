"""Shared rebar LLMConfig builder pointed at the LiteLLM gateway."""

from __future__ import annotations

from rebar.llm.config import LLMConfig
from scripts.config import Settings, get_settings


def llm_config_for(model: str, settings: Settings | None = None) -> LLMConfig:
    resolved = settings or get_settings()
    return LLMConfig(
        runner="pydantic_ai",
        model=model,
        model_provider=resolved.rebar_llm_model_provider,
        base_url=resolved.rebar_llm_base_url,
        api_key=resolved.rebar_llm_api_key,
        max_tokens=resolved.rebar_llm_max_tokens,
        temperature=resolved.rebar_llm_temperature,
    )
