"""Temporal activity that calls Bedrock through the LiteLLM gateway."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from observability import langfuse_request_metadata, record_generation, stage_span
from scripts.config import get_settings
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from services.prompts import load_prompt

# The gateway retries a throttled or failed Claude call against Claude before it answers here,
# so this budget sits on top of that and covers the gateway itself being unreachable.
MODEL_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=6,
)

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass
class ModelRequest:
    logical_model: str
    prompt: str
    max_tokens: int = 256
    execution_id: str | None = None
    task_id: str | None = None
    stage: str | None = None
    prompt_version: str | None = None
    system_prompt_name: str | None = None


@dataclass
class ModelResponse:
    logical_model: str
    content: str
    model_id: str | None = None


def _litellm_auth_headers(settings: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.litellm_master_key}"}


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = f"litellm returned {resp.status_code}: {resp.text[:500]}"
    if resp.status_code in _RETRYABLE_STATUS:
        raise ApplicationError(detail, type="LiteLLMTransient")
    raise ApplicationError(detail, type="LiteLLMRejected", non_retryable=True)


@activity.defn
async def invoke_model(req: ModelRequest) -> ModelResponse:
    settings = get_settings()
    headers = _litellm_auth_headers(settings)
    messages: list[dict[str, str]] = []
    prompt_version = req.prompt_version
    if req.system_prompt_name:
        loaded = load_prompt(req.system_prompt_name)
        messages.append({"role": "system", "content": loaded.text})
        prompt_version = loaded.version
    messages.append({"role": "user", "content": req.prompt})
    payload: dict[str, object] = {
        "model": req.logical_model,
        "messages": messages,
        "max_tokens": req.max_tokens,
    }
    metadata = langfuse_request_metadata(
        execution_id=req.execution_id,
        task_id=req.task_id,
        stage=req.stage,
        prompt_version=prompt_version,
    )
    if metadata:
        payload["metadata"] = metadata
    with stage_span(
        "llm.invoke",
        stage=req.stage or "model",
        execution_id=req.execution_id,
        task_id=req.task_id,
        prompt_version=prompt_version,
    ) as span:
        async with httpx.AsyncClient(base_url=settings.litellm_base_url, timeout=120.0) as client:
            resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
            _raise_for_status(resp)
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        model_id = data.get("model")
        usage = data.get("usage") or {}
        record_generation(
            span,
            request_model=req.logical_model,
            response_model=model_id,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
    return ModelResponse(
        logical_model=req.logical_model,
        content=content,
        model_id=model_id,
    )


async def resolve_logical_models(
    logical_models: Iterable[str],
    *,
    settings: Any | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    resolved = settings or get_settings()
    wanted = list(dict.fromkeys(logical_models))
    headers = _litellm_auth_headers(resolved)
    async with httpx.AsyncClient(
        base_url=resolved.litellm_base_url, timeout=30.0, transport=transport
    ) as client:
        resp = await client.get("/model/info", headers=headers)
        resp.raise_for_status()
        body = resp.json()

    concrete: dict[str, str] = {}
    for entry in body.get("data") or ():
        name = str(entry.get("model_name") or "")
        params = entry.get("litellm_params") or {}
        target = str(params.get("model") or "")
        if name and target:
            concrete[name] = target
    return {name: concrete[name] for name in wanted if name in concrete}
