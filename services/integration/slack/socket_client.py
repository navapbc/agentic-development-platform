"""Socket Mode transport for a tenant whose Slack app connects out from its activity worker.

The client acknowledges each envelope on its own thread as it arrives, because synchronous
activity work can block the worker's event loop for longer than the three seconds Slack waits
for an acknowledgement. The click then runs through the dispatcher on the worker's event loop,
and the outcome replaces the message through chat.update with the bot token, or reaches the
operator as an ephemeral reply through the click's response_url.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
from scripts.config import Settings
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from services.integration.credentials import ConfigError
from services.integration.slack.client import tenant_web_client
from services.integration.slack.dispatcher import (
    DispatchResult,
    PostgresDecisionRecords,
    SlackDispatcher,
    TemporalWorkflowDecisions,
    click_from_payload,
)
from services.integration.slack.messages import ephemeral_reply
from services.integration.slack.policy import response_url_allowed
from services.integration.slack.shared_app import process_shared_app
from services.orchestration.escalation import (
    SOCKET_TRANSPORT,
    SlackSettings,
    load_escalation_config,
)

log = logging.getLogger(__name__)

RESPONSE_URL_TIMEOUT_SECONDS = 5.0
CONNECT_RETRY_SECONDS = (5.0, 15.0, 60.0)
_INTERACTIVE = "interactive"
_BLOCK_ACTIONS = "block_actions"


class SocketModeTransport:
    """Route the clicks a Socket Mode client receives to the dispatcher on the worker's loop."""

    def __init__(
        self,
        client: Any,
        dispatcher: Any,
        slack_settings: Callable[[], SlackSettings | None],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._client = client
        self._dispatcher = dispatcher
        self._slack_settings = slack_settings
        self._loop = loop
        self._connecting: asyncio.Task[None] | None = None
        client.socket_mode_request_listeners.append(self._on_request)
        client.on_close_listeners.append(self._on_close)

    def connect(self) -> None:
        self._client.connect()

    def start(self) -> None:
        """Connect in the background, so a Slack outage at startup never stops the worker."""
        self._connecting = self._loop.create_task(self._connect_until_connected())

    async def _connect_until_connected(self) -> None:
        attempt = 0
        while True:
            try:
                await asyncio.to_thread(self.connect)
            except Exception as exc:  # noqa: BLE001
                delay = CONNECT_RETRY_SECONDS[min(attempt, len(CONNECT_RETRY_SECONDS) - 1)]
                log.error(
                    "Slack Socket Mode could not connect (%s), retrying in %.0f seconds",
                    type(exc).__name__,
                    delay,
                )
                attempt += 1
                await asyncio.sleep(delay)
                continue
            log.info("Slack Socket Mode client connected")
            return

    def close(self) -> None:
        if self._connecting is not None:
            self._connecting.cancel()
        self._client.close()

    def _on_request(self, client: Any, request: SocketModeRequest) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        payload = request.payload
        if request.type != _INTERACTIVE or payload.get("type") != _BLOCK_ACTIONS:
            return
        asyncio.run_coroutine_threadsafe(self._handle(payload), self._loop)

    def _on_close(self, code: int, reason: str | None = None) -> None:
        log.warning("Slack Socket Mode connection closed with code %s, reconnecting", code)

    async def _handle(self, payload: dict[str, Any]) -> None:
        try:
            result = await self._dispatcher.dispatch(click_from_payload(payload))
            await asyncio.to_thread(self._report, payload, result)
        except Exception as error:
            # The exception text can carry the response_url, which is never logged.
            log.error("Slack Socket Mode click failed with %s", type(error).__name__)

    def _report(self, payload: dict[str, Any], result: DispatchResult) -> None:
        if result.accepted:
            self._client.web_client.chat_update(
                channel=(payload.get("channel") or {}).get("id", ""),
                ts=(payload.get("message") or {}).get("ts", ""),
                text=result.reply,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": result.reply}}],
            )
            return
        slack = self._slack_settings()
        response_url = str(payload.get("response_url") or "")
        if slack is None or not response_url_allowed(response_url, slack):
            log.warning(
                "Slack Socket Mode click carried a response_url outside the tenant's hooks host"
            )
            return
        response = httpx.post(
            response_url,
            json=ephemeral_reply(result.reply),
            timeout=RESPONSE_URL_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        if response.status_code != 200:
            log.warning("Slack ephemeral reply returned status %s", response.status_code)


def build_socket_mode_client(settings: Settings, slack: SlackSettings) -> SocketModeClient:
    """Build the client against the tenant's API host, so apps.connections.open reaches it."""
    web_client = tenant_web_client(settings.slack_bot_token, slack.api_base_url, timeout=30)
    web_client.retry_handlers.append(RateLimitErrorRetryHandler())
    return SocketModeClient(app_token=settings.slack_app_token, web_client=web_client)


async def start_socket_mode(settings: Settings, temporal: Any) -> SocketModeTransport | None:
    """Connect the tenant's Socket Mode client when its escalation config selects that transport.

    Returns None for a tenant without Slack or with the webhook transport, and raises ConfigError
    when a Socket Mode tenant lacks a credential the transport needs.
    """
    shared_app = process_shared_app()
    slack = load_escalation_config(settings, shared_app=shared_app).slack
    if slack is None or slack.transport != SOCKET_TRANSPORT:
        return None
    missing = [
        name
        for name, value in (
            ("SLACK_APP_TOKEN", settings.slack_app_token),
            ("SLACK_BOT_TOKEN", settings.slack_bot_token),
            ("ESCALATION_ACTION_KEY", settings.escalation_action_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"slack.transport is socket for tenant {settings.tenant}, "
            f"but {', '.join(missing)} is not set"
        )
    from database.engine import shared_platform_engine

    from services.orchestration.alert_actions import build_tenant_alert_actions

    def active_slack() -> SlackSettings | None:
        return load_escalation_config(settings, shared_app=shared_app).slack

    dispatcher = SlackDispatcher(
        tenant=settings.tenant,
        action_key=settings.escalation_action_key.encode(),
        slack_settings=active_slack,
        decisions=TemporalWorkflowDecisions(temporal),
        records=PostgresDecisionRecords(shared_platform_engine(settings.database_url)),
        alert_actions=build_tenant_alert_actions(settings, temporal),
    )
    transport = SocketModeTransport(
        build_socket_mode_client(settings, slack),
        dispatcher,
        active_slack,
        asyncio.get_running_loop(),
    )
    transport.start()
    return transport
