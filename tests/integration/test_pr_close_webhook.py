"""PR-closed webhook: authenticated, allowlisted events signal the resolved workflow."""

from __future__ import annotations

import hashlib
import hmac
import json

from services.orchestration.webhook.pr_events import (
    PrClosedEvent,
    handle_pr_webhook,
    parse_pr_closed_event,
)

_ALLOWED = frozenset({"agentic-demo-service"})


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


def test_parse_pr_closed_event_extracts_task_id_from_branch():
    event = parse_pr_closed_event(_payload())
    assert event == PrClosedEvent(
        repo="agentic-demo-service", number=5, branch="agent/T-1", task_id="T-1", merged=True
    )


def test_parse_ignores_non_closed_actions():
    assert parse_pr_closed_event(_payload(action="opened")) is None


async def test_merged_event_signals_workflow_with_merged_true():
    resolver = _FakeResolver("wf-1")
    signaller = _FakeSignaller()
    body = _body(_payload(merged=True))

    code, response = await handle_pr_webhook(
        body,
        "",
        secret="",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 200
    assert response == {"status": "signalled", "workflow_id": "wf-1", "merged": True}
    assert signaller.calls == [("wf-1", True)]


async def test_closed_unmerged_event_signals_workflow_with_merged_false():
    resolver = _FakeResolver("wf-1")
    signaller = _FakeSignaller()
    body = _body(_payload(merged=False))

    code, _ = await handle_pr_webhook(
        body,
        "",
        secret="",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 200
    assert signaller.calls == [("wf-1", False)]


async def test_valid_signature_is_accepted():
    resolver = _FakeResolver("wf-1")
    signaller = _FakeSignaller()
    body = _body(_payload())

    code, _ = await handle_pr_webhook(
        body,
        _sign("shh", body),
        secret="shh",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 200
    assert signaller.calls == [("wf-1", True)]


async def test_invalid_signature_is_rejected():
    resolver = _FakeResolver("wf-1")
    signaller = _FakeSignaller()
    body = _body(_payload())

    code, response = await handle_pr_webhook(
        body,
        "sha256=deadbeef",
        secret="shh",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 401
    assert response == {"error": "invalid signature"}
    assert signaller.calls == []


async def test_non_allowlisted_repo_is_refused():
    resolver = _FakeResolver("wf-1")
    signaller = _FakeSignaller()
    body = _body(_payload(repo="unlisted-service"))

    code, response = await handle_pr_webhook(
        body,
        "",
        secret="",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 403
    assert response == {"error": "repo not allowlisted"}
    assert resolver.calls == []
    assert signaller.calls == []


async def test_unresolved_workflow_returns_not_found():
    resolver = _FakeResolver(None)
    signaller = _FakeSignaller()
    body = _body(_payload())

    code, response = await handle_pr_webhook(
        body,
        "",
        secret="",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 404
    assert response == {"error": "workflow not found"}
    assert signaller.calls == []


async def test_non_closed_action_is_ignored():
    resolver = _FakeResolver("wf-1")
    signaller = _FakeSignaller()
    body = _body(_payload(action="synchronize"))

    code, response = await handle_pr_webhook(
        body,
        "",
        secret="",
        allowed_repos=_ALLOWED,
        resolver=resolver,
        signaller=signaller,
    )

    assert code == 202
    assert response == {"status": "ignored"}
    assert signaller.calls == []
