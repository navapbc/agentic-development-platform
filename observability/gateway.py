"""Helpers for LLM gateway tracing and usage reconciliation.

The LiteLLM gateway forwards a request's ``metadata`` to Langfuse. These helpers
build that metadata so a generation carries the execution identifiers, the stage,
and the prompt version, joining it to the OpenTelemetry trace by ``execution_id``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def langfuse_request_metadata(
    *,
    execution_id: str | None = None,
    task_id: str | None = None,
    stage: str | None = None,
    prompt_version: str | None = None,
    extra_tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build the LiteLLM request metadata that Langfuse maps onto a generation."""
    metadata: dict[str, Any] = {}
    trace_metadata: dict[str, str] = {}
    tags: list[str] = []

    if stage:
        metadata["generation_name"] = stage
        tags.append(f"stage:{stage}")
    if prompt_version:
        metadata["version"] = str(prompt_version)
    if execution_id:
        metadata["session_id"] = execution_id
        trace_metadata["execution_id"] = execution_id
        tags.append(f"execution_id:{execution_id}")
    if task_id:
        trace_metadata["task_id"] = task_id
        tags.append(f"task:{task_id}")
    if extra_tags:
        tags.extend(extra_tags)
    if trace_metadata:
        metadata["trace_metadata"] = trace_metadata
    if tags:
        metadata["tags"] = tags
    return metadata


def langfuse_session_url(host: str, project_id: str, session_id: str) -> str:
    """Build the Langfuse session URL that groups an execution's generations."""
    if not (host and project_id and session_id):
        return ""
    return f"{host.rstrip('/')}/project/{project_id}/sessions/{session_id}"


_LITELLM_SLASH_PROVIDERS = frozenset({"bedrock"})


def _litellm_model_id(model_id: str) -> str:
    """Normalize a provider-qualified model id to the form LiteLLM prices.

    Native review provenance is ``bedrock:<model>`` (colon-separated), but LiteLLM's
    cost map addresses Bedrock as ``bedrock/<model>``. The model id itself contains
    colons (``...v1:0``), so only the leading provider separator is rewritten.
    """
    provider, sep, rest = model_id.partition(":")
    if sep and provider in _LITELLM_SLASH_PROVIDERS:
        return f"{provider}/{rest}"
    return model_id


def token_cost_usd(
    model_id: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Price a token count for a model through LiteLLM. Returns 0.0 when unknown.

    ``prompt_tokens`` is the total input the provider reports, which for the native
    Bedrock path already includes the cached tokens (pydantic-ai sums fresh input,
    cache reads, and cache writes into ``input_tokens``). Cache reads bill at a
    fraction of the input rate and cache writes at a premium, so the cached portions
    are split out and priced through LiteLLM's cache-aware parameters. Pricing the
    whole prompt at the full input rate overstates a cached run's cost several-fold.
    """
    if not model_id or (prompt_tokens <= 0 and completion_tokens <= 0):
        return 0.0
    cache_read = max(cache_read_tokens, 0)
    cache_write = max(cache_write_tokens, 0)
    fresh_prompt = max(prompt_tokens - cache_read - cache_write, 0)
    try:
        from litellm import cost_per_token

        prompt_cost, completion_cost = cost_per_token(
            model=_litellm_model_id(model_id),
            prompt_tokens=fresh_prompt,
            completion_tokens=max(completion_tokens, 0),
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )
        return float(prompt_cost) + float(completion_cost)
    except Exception:  # noqa: BLE001 - pricing must not break a stage
        return 0.0


@dataclass(frozen=True)
class Reconciliation:
    ok: bool
    prompt_token_diff: int
    completion_token_diff: int
    cost_diff: float


def reconcile_usage(
    generations: Sequence[Mapping[str, Any]],
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    cost_tolerance: float = 1e-6,
) -> Reconciliation:
    """Compare the summed generation usage against a recorded usage row."""
    summed_prompt = sum(int(item.get("prompt_tokens", 0) or 0) for item in generations)
    summed_completion = sum(int(item.get("completion_tokens", 0) or 0) for item in generations)
    summed_cost = sum(float(item.get("cost_usd", 0.0) or 0.0) for item in generations)

    prompt_diff = summed_prompt - prompt_tokens
    completion_diff = summed_completion - completion_tokens
    cost_diff = summed_cost - cost_usd
    ok = prompt_diff == 0 and completion_diff == 0 and abs(cost_diff) <= cost_tolerance
    return Reconciliation(
        ok=ok,
        prompt_token_diff=prompt_diff,
        completion_token_diff=completion_diff,
        cost_diff=cost_diff,
    )
