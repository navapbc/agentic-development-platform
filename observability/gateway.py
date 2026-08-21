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
