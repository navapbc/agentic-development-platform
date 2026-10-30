"""PR-closed handler through the inbound webhook, signalling the resolved workflow."""

from __future__ import annotations

import hashlib
import hmac
import json

from services.coordination.idempotency import InMemorySideEffectLedger
from services.integration.integrations import POLLING, WEBHOOK
from services.orchestration.webhook.inbound import handle_inbound
from services.orchestration.webhook.pr_events import (
    PrClosedEvent,
    PrClosedHandler,
    parse_pr_closed_event,
)

_ALLOWED = frozenset({"agentic-demo-service"})
_SIGNATURE_HEADER = "X-Hub-Signature-256"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_SECRET = "shh"


class _FakeResolver:
    def __init__(self, workflow_id: str | None) -> None:
        self._workflow_id = workflow_id
        self.calls: list[PrClosedEvent] = []

    async def resolve(self, event: PrClosedEvent) -> str | None:
        self.calls.append(event)
        return self._workflow_id


class _FakeSignaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def signal_pr_closed(self, workflow_id: str, *, merged: bool) -> None:
        self.calls.append((workflow_id, merged))


def _payload(
    *, repo: str = "agentic-demo-service", merged: bool = True, action: str = "closed"
) -> dict:
    return {
        "action": action,
        "repository": {"name": repo},
        "pull_request": {
            "number": 5,
            "merged": merged,
            "head": {"ref": "agent/T-1"},
        },
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _handler(
    *, secret: str = _SECRET, workflow_id: str | None = "wf-1"
) -> tuple[PrClosedHandler, _FakeResolver, _FakeSignaller]:
    resolver = _FakeResolver(workflow_id)
    signaller = _FakeSignaller()
    handler = PrClosedHandler(secret=secret, resolver=resolver, signaller=signaller)
    return handler, resolver, signaller


async def _deliver(
    handler: PrClosedHandler,
    body: bytes,
    *,
    signature: str | None = None,
    delivery: str = "d1",
    mode: str = WEBHOOK,
    ledger: InMemorySideEffectLedger | None = None,
):
    headers = {
        _SIGNATURE_HEADER: signature if signature is not None else _sign(_SECRET, body),
        _DELIVERY_HEADER: delivery,
    }
    return await handle_inbound(
        body,
        headers,
        handler=handler,
        allowed=_ALLOWED,
        ledger=ledger or InMemorySideEffectLedger(),
        mode=mode,
    )


def test_parse_pr_closed_event_extracts_task_id_from_branch():
    event = parse_pr_closed_event(_payload())
    assert event == PrClosedEvent(
        repo="agentic-demo-service", number=5, branch="agent/T-1", task_id="T-1", merged=True
    )


def test_parse_ignores_non_closed_actions():
    assert parse_pr_closed_event(_payload(action="opened")) is None


async def test_merged_event_signals_workflow_with_merged_true():
    handler, _resolver, signaller = _handler()
    code, response = await _deliver(handler, _body(_payload(merged=True)))
    assert code == 200
    assert response == {"status": "signalled", "workflow_id": "wf-1", "merged": True}
    assert signaller.calls == [("wf-1", True)]


async def test_closed_unmerged_event_signals_workflow_with_merged_false():
    handler, _resolver, signaller = _handler()
    code, _ = await _deliver(handler, _body(_payload(merged=False)))
    assert code == 200
    assert signaller.calls == [("wf-1", False)]


async def test_valid_signature_is_accepted():
    handler, _resolver, signaller = _handler()
    body = _body(_payload())
    code, _ = await _deliver(handler, body, signature=_sign(_SECRET, body))
    assert code == 200
    assert signaller.calls == [("wf-1", True)]


async def test_invalid_signature_is_rejected():
    handler, _resolver, signaller = _handler()
    body = _body(_payload())
    code, response = await _deliver(handler, body, signature="sha256=deadbeef")
    assert code == 401
    assert response == {"error": "invalid signature"}
    assert signaller.calls == []


async def test_empty_secret_refuses_the_delivery():
    handler, _resolver, signaller = _handler(secret="")
    body = _body(_payload())
    code, response = await _deliver(handler, body, signature=_sign(_SECRET, body))
    assert code == 401
    assert response == {"error": "invalid signature"}
    assert signaller.calls == []


async def test_non_allowlisted_repo_is_refused():
    handler, resolver, signaller = _handler()
    code, response = await _deliver(handler, _body(_payload(repo="unlisted-service")))
    assert code == 403
    assert response == {"error": "not allowlisted"}
    assert resolver.calls == []
    assert signaller.calls == []


async def test_unresolved_workflow_returns_not_found():
    handler, _resolver, signaller = _handler(workflow_id=None)
    code, response = await _deliver(handler, _body(_payload()))
    assert code == 404
    assert response == {"error": "workflow not found"}
    assert signaller.calls == []


async def test_non_closed_action_is_ignored():
    handler, _resolver, signaller = _handler()
    code, response = await _deliver(handler, _body(_payload(action="synchronize")))
    assert code == 202
    assert response == {"status": "ignored"}
    assert signaller.calls == []


async def test_polling_mode_disables_the_webhook():
    handler, resolver, signaller = _handler()
    code, response = await _deliver(handler, _body(_payload()), mode=POLLING)
    assert code == 202
    assert response == {"status": "webhook disabled", "integration": "pr_closed"}
    assert resolver.calls == []
    assert signaller.calls == []


async def test_redelivered_event_is_a_no_op_that_replays_the_outcome():
    handler, _resolver, signaller = _handler()
    ledger = InMemorySideEffectLedger()
    body = _body(_payload())
    first = await _deliver(handler, body, delivery="dup", ledger=ledger)
    second = await _deliver(handler, body, delivery="dup", ledger=ledger)
    assert first == (200, {"status": "signalled", "workflow_id": "wf-1", "merged": True})
    assert second[0] == 200
    assert second[1]["duplicate"] is True
    assert signaller.calls == [("wf-1", True)]
