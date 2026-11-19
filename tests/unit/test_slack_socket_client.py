"""The activity worker's Socket Mode client acknowledges each envelope on its own thread, hands
the click to the dispatcher on the worker's event loop, and reports the outcome in Slack."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from services.integration import tenant_config
from services.integration.credentials import ConfigError
from services.integration.slack.dispatcher import DispatchResult, Refusal, SlackClick
from services.integration.slack.socket_client import SocketModeTransport, start_socket_mode
from services.orchestration.escalation import SlackSettings
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.web import WebClient
from tests.support import hermetic_settings

SOCKET_ESCALATION = """config_version: 1
channels: [jira, slack]
slack:
  transport: socket
  channel: C0123456789
  organization_id: T0123456789
  app_id: A0123456789
  profile: private
  content: full
"""
GOVSLACK_ESCALATION = (
    SOCKET_ESCALATION.replace("profile: private", "profile: government").replace(
        "content: full", "content: minimal"
    )
    + "  api_base_url: https://slack-gov.com/api/\n"
    + "  approved_hosts: [slack-gov.com, hooks.slack-gov.com]\n"
)
WEBHOOK_ESCALATION = SOCKET_ESCALATION.replace("transport: socket", "transport: webhook")
SLACK = SlackSettings(
    transport="socket",
    channel="C0123456789",
    organization_id="T0123456789",
    app_id="A0123456789",
    profile="private",
    content="full",
)
ALLOWED_RESPONSE_URL = "https://hooks.slack.com/actions/T0123456789/1/abc"
ACCEPTED = DispatchResult(
    reply="Approved by slack:T0123456789/U0123456789.",
    decision="approve",
    actor="slack:T0123456789/U0123456789",
    applied=True,
)
REFUSED = DispatchResult(
    reply="This park is no longer waiting on a decision.", refusal=Refusal.NOT_WAITING
)


class _WebClient:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.updated = threading.Event()

    def chat_update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        self.updated.set()


class _SocketClient:
    def __init__(self) -> None:
        self.socket_mode_request_listeners: list[Any] = []
        self.on_close_listeners: list[Any] = []
        self.web_client = _WebClient()
        self.acks: list[str] = []

    def send_socket_mode_response(self, response: Any) -> None:
        self.acks.append(response.envelope_id)

    def deliver(self, request: SocketModeRequest) -> None:
        for listener in self.socket_mode_request_listeners:
            listener(self, request)


class _Dispatcher:
    def __init__(self, result: DispatchResult) -> None:
        self.result = result
        self.clicks: list[SlackClick] = []

    async def dispatch(self, click: SlackClick) -> DispatchResult:
        self.clicks.append(click)
        return self.result


def _click_request(
    response_url: str = ALLOWED_RESPONSE_URL, envelope_type: str = "interactive"
) -> SocketModeRequest:
    return SocketModeRequest(
        type=envelope_type,
        envelope_id="envelope-1",
        payload={
            "type": "block_actions",
            "user": {"id": "U0123456789", "team_id": "T0123456789"},
            "channel": {"id": "C0123456789"},
            "message": {"ts": "1700000000.000100"},
            "response_url": response_url,
            "actions": [{"action_id": "approve", "value": "signed-token"}],
        },
    )


async def _deliver(client: _SocketClient, request: SocketModeRequest) -> None:
    """Deliver the request and wait for every click handler the transport scheduled.

    run_coroutine_threadsafe creates its task in a later loop callback, so asyncio.all_tasks()
    can miss it. Recording the futures it returns waits on exactly the scheduled handlers.
    """
    scheduled: list[concurrent.futures.Future[None]] = []
    schedule = asyncio.run_coroutine_threadsafe

    def record(coro: Any, loop: asyncio.AbstractEventLoop) -> concurrent.futures.Future[None]:
        future = schedule(coro, loop)
        scheduled.append(future)
        return future

    with mock.patch.object(asyncio, "run_coroutine_threadsafe", record):
        await asyncio.to_thread(client.deliver, request)
    await asyncio.gather(*(asyncio.wrap_future(future) for future in scheduled))


@pytest.fixture
def ephemeral_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        posts.append({"url": url, **kwargs})
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", post)
    return posts


async def test_a_click_is_acknowledged_and_reaches_the_dispatcher_once(ephemeral_posts) -> None:
    client, dispatcher = _SocketClient(), _Dispatcher(ACCEPTED)
    SocketModeTransport(client, dispatcher, lambda: SLACK, asyncio.get_running_loop())

    await _deliver(client, _click_request())

    assert client.acks == ["envelope-1"]
    assert [(click.action, click.user, click.channel) for click in dispatcher.clicks] == [
        ("approve", "U0123456789", "C0123456789")
    ]


async def test_an_envelope_other_than_a_click_is_acknowledged_and_ignored(ephemeral_posts) -> None:
    client, dispatcher = _SocketClient(), _Dispatcher(ACCEPTED)
    SocketModeTransport(client, dispatcher, lambda: SLACK, asyncio.get_running_loop())

    await _deliver(client, _click_request(envelope_type="events_api"))

    assert client.acks == ["envelope-1"]
    assert dispatcher.clicks == []


def test_an_envelope_is_acknowledged_while_the_worker_event_loop_is_blocked() -> None:
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    release = threading.Event()
    loop.call_soon_threadsafe(release.wait, 10)
    client, dispatcher = _SocketClient(), _Dispatcher(ACCEPTED)
    SocketModeTransport(client, dispatcher, lambda: SLACK, loop)
    try:
        started = time.monotonic()
        client.deliver(_click_request())

        assert client.acks == ["envelope-1"]
        assert time.monotonic() - started < 3
        assert dispatcher.clicks == []

        release.set()
        assert client.web_client.updated.wait(5)
        assert len(dispatcher.clicks) == 1
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        runner.join(5)
        loop.close()


async def test_an_accepted_decision_replaces_the_message_without_buttons(ephemeral_posts) -> None:
    client = _SocketClient()
    SocketModeTransport(client, _Dispatcher(ACCEPTED), lambda: SLACK, asyncio.get_running_loop())

    await _deliver(client, _click_request())

    assert client.web_client.updates == [
        {
            "channel": "C0123456789",
            "ts": "1700000000.000100",
            "text": ACCEPTED.reply,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": ACCEPTED.reply}}],
        }
    ]
    assert ephemeral_posts == []


async def test_a_refusal_replies_ephemerally_only_to_an_allowed_response_url(
    ephemeral_posts,
) -> None:
    client = _SocketClient()
    SocketModeTransport(client, _Dispatcher(REFUSED), lambda: SLACK, asyncio.get_running_loop())

    await _deliver(client, _click_request())
    await _deliver(client, _click_request("https://hooks.example.net/actions/T0123456789/1/abc"))

    assert ephemeral_posts == [
        {
            "url": ALLOWED_RESPONSE_URL,
            "json": {
                "response_type": "ephemeral",
                "replace_original": False,
                "text": REFUSED.reply,
            },
            "timeout": 5.0,
            "follow_redirects": False,
        }
    ]
    assert client.web_client.updates == []


def test_a_dropped_connection_is_logged(caplog) -> None:
    client = _SocketClient()
    SocketModeTransport(client, _Dispatcher(ACCEPTED), lambda: SLACK, asyncio.new_event_loop())

    with caplog.at_level(logging.WARNING):
        for listener in client.on_close_listeners:
            listener(1006, None)

    assert "Socket Mode connection closed" in caplog.text


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    env = tmp_path / "env"
    env.mkdir()
    monkeypatch.setenv("PLATFORM_ENV_CONFIG_DIR", str(env))
    tenant_config.reset()
    yield env
    tenant_config.reset()


def _settings(**overrides: str):
    values = {
        "tenant": "adp",
        "slack_bot_token": "xoxb-bot",
        "slack_app_token": "xapp-app",
        "escalation_action_key": "action-key",
        **overrides,
    }
    return hermetic_settings(**values)


async def test_a_socket_tenant_without_an_app_token_refuses_to_start(environment) -> None:
    (environment / "escalation.yaml").write_text(SOCKET_ESCALATION)

    with pytest.raises(ConfigError, match="SLACK_APP_TOKEN"):
        await start_socket_mode(_settings(slack_app_token=""), object())


async def test_a_webhook_tenant_starts_no_socket_mode_client(environment) -> None:
    (environment / "escalation.yaml").write_text(WEBHOOK_ESCALATION)

    assert await start_socket_mode(_settings(slack_app_token=""), object()) is None


async def test_a_govslack_tenant_opens_its_connection_on_slack_gov(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    (environment / "escalation.yaml").write_text(GOVSLACK_ESCALATION)
    requested: list[str] = []

    def perform(self: WebClient, *, url: str, args: dict[str, Any]) -> dict[str, Any]:
        requested.append(url)
        return {"status": 200, "headers": {}, "body": '{"ok": false, "error": "invalid_auth"}'}

    monkeypatch.setattr(WebClient, "_perform_urllib_http_request", perform)
    logging.getLogger("slack_sdk").setLevel(logging.DEBUG)

    transport = await start_socket_mode(_settings(), object())
    assert transport is not None
    for _ in range(100):
        if requested:
            break
        await asyncio.sleep(0.01)
    transport.close()

    assert requested[0] == "https://slack-gov.com/api/apps.connections.open"
    assert logging.getLogger("slack_sdk").getEffectiveLevel() == logging.INFO


async def test_a_slack_outage_at_startup_does_not_stop_the_worker(
    environment, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    (environment / "escalation.yaml").write_text(GOVSLACK_ESCALATION)
    monkeypatch.setattr("services.integration.slack.socket_client.CONNECT_RETRY_SECONDS", (0.01,))
    attempts: list[str] = []

    def perform(self: WebClient, *, url: str, args: dict[str, Any]) -> dict[str, Any]:
        attempts.append(url)
        raise ConnectionError("slack is down")

    monkeypatch.setattr(WebClient, "_perform_urllib_http_request", perform)

    with caplog.at_level(logging.ERROR, logger="services.integration.slack.socket_client"):
        transport = await start_socket_mode(_settings(), object())
        for _ in range(200):
            if len(attempts) >= 2:
                break
            await asyncio.sleep(0.01)
        transport.close()

    assert len(attempts) >= 2
    assert "could not connect (ConnectionError)" in caplog.text
