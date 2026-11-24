"""Socket Mode delivery of the shared Slack app's clicks to the inbound webhook.

With SLACK_SHARED_APP_TOKEN set, the webhook opens a Socket Mode connection out to Slack under the
shared app's app-level token, so a click needs no inbound URL. Slack authenticates the connection,
so a payload it delivers carries no signature, and each one passes through the shared route's
``decide``, which applies the same app ID, channel, retention, action key, and allowlist checks as
a request URL click. The connection is built against the shared app's API host without the bot
token, which only the Slack posting service holds. It waits for the snapshot's shared app, and
reconnects after an outage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from services.integration.slack.shared_app import SharedSlackApp
from services.integration.slack.socket_client import CONNECT_RETRY_SECONDS
from services.orchestration.webhook.slack_events import SharedAppSource, SharedSlackInteractivity

log = logging.getLogger(__name__)

INTERACTIVE = "interactive"
BLOCK_ACTIONS = "block_actions"
SHARED_APP_WAIT_SECONDS = 5.0

SocketClientFactory = Callable[[str, SharedSlackApp], Any]


def socket_mode_client(app_token: str, shared_app: SharedSlackApp) -> Any:
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient

    return SocketModeClient(
        app_token=app_token, web_client=WebClient(base_url=shared_app.api_base_url)
    )


class SharedSlackSocket:
    """Receive the shared app's clicks over Socket Mode and decide them on the shared route."""

    def __init__(
        self,
        route: SharedSlackInteractivity,
        app_token: str,
        shared_app: SharedAppSource,
        loop: asyncio.AbstractEventLoop,
        *,
        client_factory: SocketClientFactory = socket_mode_client,
    ) -> None:
        self._route = route
        self._app_token = app_token
        self._shared_app = shared_app
        self._loop = loop
        self._client_factory = client_factory
        self._client: Any = None
        self._connecting: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Connect in the background, so a Slack outage at startup never stops the webhook."""
        self._connecting = self._loop.create_task(self._connect_until_connected())

    async def _connect_until_connected(self) -> None:
        shared_app = self._shared_app()
        if shared_app is None:
            log.error("the shared Slack app is not configured, so Socket Mode waits for it")
        while shared_app is None:
            await asyncio.sleep(SHARED_APP_WAIT_SECONDS)
            shared_app = self._shared_app()
        self._client = self._client_factory(self._app_token, shared_app)
        self._client.socket_mode_request_listeners.append(self._on_request)
        attempt = 0
        while True:
            try:
                await asyncio.to_thread(self._client.connect)
            except Exception as exc:  # noqa: BLE001
                delay = CONNECT_RETRY_SECONDS[min(attempt, len(CONNECT_RETRY_SECONDS) - 1)]
                log.error(
                    "the shared Slack app's Socket Mode could not connect (%s), retrying in %.0f "
                    "seconds",
                    type(exc).__name__,
                    delay,
                )
                attempt += 1
                await asyncio.sleep(delay)
                continue
            log.info("the shared Slack app's Socket Mode client connected")
            return

    def close(self) -> None:
        if self._connecting is not None:
            self._connecting.cancel()
        if self._client is not None:
            self._client.close()

    def _on_request(self, client: Any, request: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        payload = request.payload
        if request.type != INTERACTIVE or payload.get("type") != BLOCK_ACTIONS:
            return
        asyncio.run_coroutine_threadsafe(self.handle(payload), self._loop)

    async def handle(self, payload: dict[str, Any]) -> None:
        try:
            reply = await self._route.decide(payload)
            if reply.follow_up is not None:
                await self._route.send_follow_up(reply.follow_up)
        except Exception as error:  # noqa: BLE001
            # The exception text can carry the response_url, which is never logged.
            log.error(
                "a shared Slack app click over Socket Mode failed with %s", type(error).__name__
            )
