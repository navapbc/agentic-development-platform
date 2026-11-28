"""The shared Slack app's clicks reach the webhook's shared route over Socket Mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from services.integration.slack.shared_app import SharedSlackApp
from services.orchestration.webhook import slack_socket
from services.orchestration.webhook.slack_events import FollowUp, SlackReply
from services.orchestration.webhook.slack_socket import SharedSlackSocket, socket_mode_client

SHARED = SharedSlackApp("A0SHARED001", "E0SHARED001", "https://slack-gov.com/api/")


@dataclass
class _Request:
    type: str
    payload: dict[str, Any]
    envelope_id: str = "envelope-1"


@dataclass
class _Client:
    socket_mode_request_listeners: list[Any] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)
    connected: int = 0

    def connect(self) -> None:
        self.connected += 1

    def send_socket_mode_response(self, response: Any) -> None:
        self.acknowledged.append(response.envelope_id)

    def close(self) -> None:
        return None


class _Route:
    def __init__(self) -> None:
        self.decided: list[dict[str, Any]] = []
        self.followed: list[FollowUp] = []

    async def decide(self, payload: dict[str, Any]) -> SlackReply:
        self.decided.append(payload)
        return SlackReply(200, follow_up=FollowUp("https://hooks.slack.com/actions/1", {}))

    async def send_follow_up(self, follow_up: FollowUp) -> None:
        self.followed.append(follow_up)


async def _until(condition) -> bool:
    for _ in range(200):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_every_envelope_is_acknowledged_and_only_block_actions_are_decided() -> None:
    client, route = _Client(), _Route()
    socket = SharedSlackSocket(
        route,
        "xapp-shared",
        lambda: SHARED,
        asyncio.get_running_loop(),
        client_factory=lambda token, app: client,
    )
    socket.start()
    assert await _until(lambda: client.connected == 1)
    [listener] = client.socket_mode_request_listeners

    listener(client, _Request("events_api", {"type": "event_callback"}, "envelope-1"))
    listener(client, _Request("interactive", {"type": "view_submission"}, "envelope-2"))
    listener(client, _Request("interactive", {"type": "block_actions"}, "envelope-3"))

    assert await _until(lambda: route.followed)
    assert client.acknowledged == ["envelope-1", "envelope-2", "envelope-3"]
    assert route.decided == [{"type": "block_actions"}]
    socket.close()


async def test_the_connection_waits_for_the_shared_app_and_uses_its_api_host(monkeypatch) -> None:
    monkeypatch.setattr(slack_socket, "SHARED_APP_WAIT_SECONDS", 0.01)
    snapshot: dict[str, SharedSlackApp | None] = {"app": None}
    built: list[tuple[str, SharedSlackApp]] = []

    def factory(token: str, app: SharedSlackApp) -> _Client:
        built.append((token, app))
        return _Client()

    socket = SharedSlackSocket(
        _Route(),
        "xapp-shared",
        lambda: snapshot["app"],
        asyncio.get_running_loop(),
        client_factory=factory,
    )
    socket.start()
    await asyncio.sleep(0.05)
    assert built == []

    snapshot["app"] = SHARED

    assert await _until(lambda: built == [("xapp-shared", SHARED)])
    socket.close()


def test_the_socket_mode_client_holds_the_app_token_and_no_bot_token() -> None:
    client = socket_mode_client("xapp-shared", SHARED)

    assert client.app_token == "xapp-shared"
    assert client.web_client.token is None
    assert client.web_client.base_url == SHARED.api_base_url
    client.close()
