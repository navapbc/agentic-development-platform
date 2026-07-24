"""Temporal activity that calls Bedrock through the LiteLLM gateway."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from scripts.config import get_settings
from temporalio import activity


@dataclass
class ModelRequest:
    logical_model: str
    prompt: str
    max_tokens: int = 256


@dataclass
class ModelResponse:
    logical_model: str
    content: str
    model_id: str | None = None


@activity.defn
async def invoke_model(req: ModelRequest) -> ModelResponse:
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.litellm_master_key}"}
    payload = {
        "model": req.logical_model,
        "messages": [{"role": "user", "content": req.prompt}],
        "max_tokens": req.max_tokens,
    }
    async with httpx.AsyncClient(base_url=settings.litellm_base_url, timeout=120.0) as client:
        resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    return ModelResponse(
        logical_model=req.logical_model,
        content=content,
        model_id=data.get("model"),
    )
