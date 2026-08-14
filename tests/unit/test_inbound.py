"""Hermetic tests for the shared inbound webhook dispatch contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.integrations import HYBRID, POLLING, WEBHOOK
from services.orchestration.webhook.inbound import DispatchNotReady, handle_inbound

_ALLOWED = frozenset({"widget"})
_DELIVERY = "X-Delivery"


@dataclass
class _Event:
    name: str


class _FakeHandler:
    name = "fake"

    def __init__(
        self, *, signature_ok: bool = True, parsed: _Event | None = None, ready: bool = True
    ) -> None:
        self._signature_ok = signature_ok
        self._parsed = parsed if parsed is not None else _Event("widget")
        self._ready = ready
        self.dispatched: list[_Event] = []

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return self._signature_ok

    def delivery_id(self, headers: Mapping[str, str]) -> str:
        return headers.get(_DELIVERY, "")

    def parse(self, payload: dict[str, Any]) -> _Event | None:
        if payload.get("skip"):
            return None
        return self._parsed

    def allowlisted(self, event: _Event, allowed: frozenset[str]) -> bool:
        return event.name in allowed

    async def dispatch(self, event: _Event) -> dict[str, Any]:
        if not self._ready:
            raise DispatchNotReady("target not found")
        self.dispatched.append(event)
        return {"status": "handled", "name": event.name}


def _body(payload: dict[str, Any] | None = None) -> bytes:
    return json.dumps(payload or {}).encode()


async def _run(handler: _FakeHandler, *, mode: str, headers: dict[str, str], body: bytes):
    return await handle_inbound(
        body,
        headers,
        handler=handler,
        allowed=_ALLOWED,
        ledger=InMemorySideEffectLedger(),
        mode=mode,
    )


async def test_polling_mode_refuses_with_202() -> None:
    handler = _FakeHandler()
    code, payload = await _run(handler, mode=POLLING, headers={_DELIVERY: "d1"}, body=_body())
    assert code == 202
    assert payload == {"status": "webhook disabled", "integration": "fake"}
    assert handler.dispatched == []


async def test_hybrid_mode_dispatches() -> None:
    handler = _FakeHandler()
    code, payload = await _run(handler, mode=HYBRID, headers={_DELIVERY: "d1"}, body=_body())
    assert code == 200
    assert payload == {"status": "handled", "name": "widget"}
    assert len(handler.dispatched) == 1


async def test_invalid_signature_is_rejected() -> None:
    handler = _FakeHandler(signature_ok=False)
    code, payload = await _run(handler, mode=WEBHOOK, headers={_DELIVERY: "d1"}, body=_body())
    assert code == 401
    assert payload == {"error": "invalid signature"}


async def test_invalid_json_is_rejected() -> None:
    handler = _FakeHandler()
    code, payload = await _run(handler, mode=WEBHOOK, headers={_DELIVERY: "d1"}, body=b"{ not json")
    assert code == 400
    assert payload == {"error": "invalid json"}


async def test_ignored_event_returns_202() -> None:
    handler = _FakeHandler()
    code, payload = await _run(
        handler, mode=WEBHOOK, headers={_DELIVERY: "d1"}, body=_body({"skip": True})
    )
    assert code == 202
    assert payload == {"status": "ignored"}


async def test_non_allowlisted_event_is_refused() -> None:
    handler = _FakeHandler(parsed=_Event("other"))
    code, payload = await _run(handler, mode=WEBHOOK, headers={_DELIVERY: "d1"}, body=_body())
    assert code == 403
    assert payload == {"error": "not allowlisted"}


async def test_missing_delivery_id_is_rejected() -> None:
    handler = _FakeHandler()
    code, payload = await _run(handler, mode=WEBHOOK, headers={}, body=_body())
    assert code == 400
    assert payload == {"error": "missing delivery id"}


async def test_dispatch_not_ready_returns_404() -> None:
    handler = _FakeHandler(ready=False)
    code, payload = await _run(handler, mode=WEBHOOK, headers={_DELIVERY: "d1"}, body=_body())
    assert code == 404
    assert payload == {"error": "target not found"}


async def test_duplicate_delivery_replays_without_redispatch() -> None:
    handler = _FakeHandler()
    ledger = InMemorySideEffectLedger()
    headers = {_DELIVERY: "d1"}
    first = await handle_inbound(
        _body(), headers, handler=handler, allowed=_ALLOWED, ledger=ledger, mode=WEBHOOK
    )
    second = await handle_inbound(
        _body(), headers, handler=handler, allowed=_ALLOWED, ledger=ledger, mode=WEBHOOK
    )
    assert first == (200, {"status": "handled", "name": "widget"})
    assert second == (200, {"duplicate": True, "status": "handled", "name": "widget"})
    assert len(handler.dispatched) == 1
