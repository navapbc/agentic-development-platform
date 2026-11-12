"""Read an execution's generations from the Langfuse Observations API v2.

The v4 events_only deployment disables the legacy trace and session read
endpoints. The Observations API v2 stays available in that mode and returns per
generation usage and cost, so reconciliation reads the generations from it and
selects the ones whose session matches the execution.
"""

from __future__ import annotations

from typing import Any

_ENDPOINT = "/api/public/v2/observations"


def generation_usage(observation: dict[str, Any]) -> dict[str, float]:
    """Reduce one Langfuse observation to prompt tokens, completion tokens, and cost."""
    usage = observation.get("usageDetails") or {}
    cost = observation.get("costDetails") or {}
    prompt = (
        usage.get("input") or usage.get("prompt_tokens") or observation.get("promptTokens") or 0
    )
    completion = (
        usage.get("output")
        or usage.get("completion_tokens")
        or observation.get("completionTokens")
        or 0
    )
    total_cost = cost.get("total") if isinstance(cost, dict) else None
    if total_cost is None:
        total_cost = observation.get("calculatedTotalCost") or observation.get("totalPrice") or 0.0
    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "cost_usd": float(total_cost or 0.0),
    }


def generation_row(observation: dict[str, Any]) -> dict[str, Any]:
    """Reduce one observation to usage plus its stage and model for a per-stage rollup.

    The stage is the Langfuse generation name, which the header-propagation seam sets
    from the execution stage (``stage.value``, hyphenated). The model is the resolved
    model id. Both are carried so a standalone rollup can attribute the usage back to
    the stage whose in-process row rebar returned as zero.
    """
    usage = generation_usage(observation)
    row: dict[str, Any] = dict(usage)
    row["stage"] = str(observation.get("name") or "")
    row["model_id"] = observation.get("model") or None
    return row


def _iter_session_observations(
    *,
    client: Any,
    session_id: str,
    page_cap: int,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(page_cap):
        params: dict[str, Any] = {"type": "GENERATION", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        response = client.get(_ENDPOINT, params=params)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or []
        observations.extend(item for item in data if item.get("sessionId") == session_id)
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor or not data:
            break
    return observations


def fetch_session_generations(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    session_id: str,
    timeout: float = 15.0,
    page_cap: int = 200,
    client: Any | None = None,
) -> list[dict[str, float]]:
    """Return the per-generation usage for one execution session from Langfuse."""
    import httpx

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            base_url=host.rstrip("/"), timeout=timeout, auth=(public_key, secret_key)
        )
    try:
        observations = _iter_session_observations(
            client=client, session_id=session_id, page_cap=page_cap
        )
    finally:
        if owns_client:
            client.close()
    return [generation_usage(observation) for observation in observations]


def fetch_session_generation_rows(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    session_id: str,
    timeout: float = 15.0,
    page_cap: int = 200,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Return the per-generation usage with stage and model for one execution session."""
    import httpx

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            base_url=host.rstrip("/"), timeout=timeout, auth=(public_key, secret_key)
        )
    try:
        observations = _iter_session_observations(
            client=client, session_id=session_id, page_cap=page_cap
        )
    finally:
        if owns_client:
            client.close()
    return [generation_row(observation) for observation in observations]
