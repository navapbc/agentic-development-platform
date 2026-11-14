"""Read an execution's generations from the Langfuse Observations API v2.

The v4 events_only deployment disables the legacy trace and session read
endpoints. The Observations API v2 stays available in that mode and returns per
generation usage and cost, so reconciliation reads the generations of each
execution session from it with a server side session filter.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

_ENDPOINT = "/api/public/v2/observations"
_PAGE_LIMIT = 50
# The API returns only the core field group unless asked, which carries no usage, cost, or model.
_FIELDS = "core,basic,usage,model"


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


def observation_params(session_id: str = "") -> dict[str, Any]:
    """The Observations API query for generations, narrowed server side to one session.

    The ``filter`` parameter takes precedence over the simple query parameters, so the
    session filter repeats the generation type condition.
    """
    params: dict[str, Any] = {"type": "GENERATION", "limit": _PAGE_LIMIT, "fields": _FIELDS}
    if session_id:
        params["filter"] = json.dumps(
            [
                {"type": "string", "column": "type", "operator": "=", "value": "GENERATION"},
                {"type": "string", "column": "sessionId", "operator": "=", "value": session_id},
            ]
        )
    return params


def _iter_observations(
    *,
    client: Any,
    page_cap: int,
    keep: Callable[[dict[str, Any]], bool],
    session_id: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Return the kept observations and whether the read reached the last page."""
    observations: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(page_cap):
        params = observation_params(session_id)
        if cursor:
            params["cursor"] = cursor
        response = client.get(_ENDPOINT, params=params)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or []
        observations.extend(item for item in data if keep(item))
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor or not data:
            return observations, True
    return observations, False


def _client(host: str, public_key: str, secret_key: str, timeout: float) -> Any:
    import httpx

    return httpx.Client(base_url=host.rstrip("/"), timeout=timeout, auth=(public_key, secret_key))


def _read_session(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    timeout: float,
    page_cap: int,
    client: Any | None,
    session_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    session = client or _client(host, public_key, secret_key, timeout)
    try:
        return _iter_observations(
            client=session,
            page_cap=page_cap,
            keep=lambda item: item.get("sessionId") == session_id,
            session_id=session_id,
        )
    finally:
        if client is None:
            session.close()


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
    observations, _complete = _read_session(
        host=host,
        public_key=public_key,
        secret_key=secret_key,
        timeout=timeout,
        page_cap=page_cap,
        client=client,
        session_id=session_id,
    )
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
    observations, _complete = _read_session(
        host=host,
        public_key=public_key,
        secret_key=secret_key,
        timeout=timeout,
        page_cap=page_cap,
        client=client,
        session_id=session_id,
    )
    return [generation_row(observation) for observation in observations]


@dataclass(frozen=True)
class SessionGenerations:
    """The generation rows of several sessions, and the sessions whose read stopped early.

    A session in ``incomplete`` had more pages than the page cap allows, so its rows are a
    prefix of its generations and cannot be compared against recorded usage.
    """

    rows: dict[str, list[dict[str, Any]]]
    incomplete: frozenset[str]


def fetch_generation_rows_by_session(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    session_ids: Collection[str],
    timeout: float = 15.0,
    page_cap: int = 200,
    client: Any | None = None,
) -> SessionGenerations:
    """Return the per-generation rows of several execution sessions, one filtered read each."""
    session = client or _client(host, public_key, secret_key, timeout)
    rows: dict[str, list[dict[str, Any]]] = {}
    incomplete: set[str] = set()
    try:
        for session_id in sorted(set(session_ids)):
            observations, complete = _read_session(
                host=host,
                public_key=public_key,
                secret_key=secret_key,
                timeout=timeout,
                page_cap=page_cap,
                client=session,
                session_id=session_id,
            )
            rows[session_id] = [generation_row(observation) for observation in observations]
            if not complete:
                incomplete.add(session_id)
    finally:
        if client is None:
            session.close()
    return SessionGenerations(rows=rows, incomplete=frozenset(incomplete))
