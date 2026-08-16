"""Shared inbound webhook service: verify, allowlist, deduplicate by delivery id, dispatch."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from services.coordination.idempotency import SideEffectLedger, SideEffectRef
from services.integration.integrations import HYBRID, WEBHOOK


class DispatchNotReady(Exception):
    """Raised by a handler when the delivery cannot be completed yet and must not be recorded."""


class IntegrationHandler(Protocol):
    name: str

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool: ...

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str: ...

    def parse(self, payload: dict[str, Any]) -> Any | None: ...

    def allowlisted(self, event: Any, allowed: frozenset[str]) -> bool: ...

    async def dispatch(self, event: Any) -> dict[str, Any]: ...


async def handle_inbound(
    body: bytes,
    headers: Mapping[str, str],
    *,
    handler: IntegrationHandler,
    allowed: frozenset[str],
    ledger: SideEffectLedger,
    mode: str,
) -> tuple[int, dict[str, Any]]:
    if mode not in (WEBHOOK, HYBRID):
        return 202, {"status": "webhook disabled", "integration": handler.name}
    if not handler.verify(body, headers):
        return 401, {"error": "invalid signature"}
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return 400, {"error": "invalid json"}
    if not isinstance(payload, dict):
        return 400, {"error": "invalid payload"}
    event = handler.parse(payload)
    if event is None:
        return 202, {"status": "ignored"}
    if not handler.allowlisted(event, allowed):
        return 403, {"error": "not allowlisted"}
    delivery_id = handler.delivery_id(headers, payload)
    if not delivery_id:
        return 400, {"error": "missing delivery id"}
    ref = SideEffectRef(
        task_id="",
        execution_id="",
        operation=f"webhook:{handler.name}",
        target=delivery_id,
    )
    try:
        outcome = await ledger.run_once(ref, lambda: handler.dispatch(event))
    except DispatchNotReady as exc:
        return 404, {"error": str(exc) or "target not found"}
    result = outcome.result if isinstance(outcome.result, dict) else {}
    if outcome.replayed:
        return 200, {"duplicate": True, **result}
    return 200, result
