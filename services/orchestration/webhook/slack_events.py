"""Accept Slack interactivity on the inbound webhook and answer through response_url.

A tenant on its own Slack app is served at /webhook/slack/<tenant>, and every tenant on the shared
Slack app at /webhook/slack/_shared. The request is refused with 401 when its
X-Slack-Request-Timestamp is missing, is not a number, or is more than five minutes from the server
clock, and when its X-Slack-Signature is not the v0 HMAC-SHA256 of the timestamp and the raw body
under the route's signing secret, the tenant's or the shared app's. Only a verified body is decoded.
A click is decided by a tenant's dispatcher before the response, and the message is updated
afterwards through the click's response_url, which is called only when it belongs to the Slack
hooks origin of the tenant's API host, without following redirects.

A tenant route serves its tenant only while the tenant's Slack settings name webhook transport and
the tenant's own app. The shared route decides each click under the tenant on the shared app whose
escalation.yaml names the click's channel, read from the refreshed channel index, and refuses a
click from a channel that routes to no tenant with an ephemeral reply through a response_url on the
shared app's hooks host.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import httpx

from services.integration.slack.dispatcher import (
    DecisionRecords,
    DispatchResult,
    SlackDispatcher,
    click_from_payload,
    refuse_unrouted,
)
from services.integration.slack.messages import ephemeral_reply
from services.integration.slack.policy import hooks_url_allowed, response_url_allowed
from services.integration.slack.shared_app import SharedSlackApp
from services.orchestration.escalation import (
    SHARED_APP,
    WEBHOOK_TRANSPORT,
    SlackSettings,
    host_of,
)

log = logging.getLogger(__name__)

TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SIGNATURE_HEADER = "X-Slack-Signature"
MAX_CLOCK_SKEW_SECONDS = 300
FOLLOW_UP_TIMEOUT_SECONDS = 5.0
FOLLOW_UP_CONCURRENCY = 4
BLOCK_ACTIONS = "block_actions"

SlackSettingsSource = Callable[[str], SlackSettings | None]
ChannelTenantSource = Callable[[str], str | None]
SharedAppSource = Callable[[], SharedSlackApp | None]
RetainedHolderSource = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class FollowUp:
    response_url: str
    message: dict[str, Any]


@dataclass(frozen=True)
class SlackReply:
    status: int
    error: str = ""
    follow_up: FollowUp | None = None


def timestamp_fresh(timestamp: str, now: float) -> bool:
    try:
        sent = int(timestamp)
    except ValueError:
        return False
    return abs(now - sent) <= MAX_CLOCK_SKEW_SECONDS


def signature_valid(secret: str, timestamp: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode(errors="replace"))


def decode_payload(body: bytes) -> dict[str, Any] | None:
    try:
        form = parse_qs(body.decode("utf-8"), strict_parsing=True)
        payload = json.loads(form["payload"][0])
    except UnicodeDecodeError, ValueError, KeyError:
        return None
    return payload if isinstance(payload, dict) else None


def follow_up_message(result: DispatchResult) -> dict[str, Any]:
    """Replace the message with the outcome after an accepted decision, else reply ephemerally."""
    if result.accepted:
        return {
            "replace_original": True,
            "text": result.reply,
            "blocks": [{"type": "section", "text": {"type": "plain_text", "text": result.reply}}],
        }
    return ephemeral_reply(result.reply)


def post_follow_up(follow_up: FollowUp) -> None:
    response = httpx.post(
        follow_up.response_url,
        json=follow_up.message,
        timeout=FOLLOW_UP_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    response.raise_for_status()


def verified_payload(
    secret: str, body: bytes, headers: Mapping[str, str]
) -> dict[str, Any] | SlackReply:
    """Return the decoded payload of a request signed under the secret, else the refusal reply."""
    timestamp = headers.get(TIMESTAMP_HEADER)
    if timestamp is None or not timestamp_fresh(timestamp, time.time()):
        return SlackReply(401, "invalid timestamp")
    if not signature_valid(secret, timestamp, body, headers.get(SIGNATURE_HEADER)):
        return SlackReply(401, "invalid signature")
    payload = decode_payload(body)
    if payload is None:
        return SlackReply(400, "invalid payload")
    return payload


def shared_app_settings(slack: SlackSettingsSource, tenant: str) -> SlackSettings | None:
    settings = slack(tenant)
    if settings is None or settings.app != SHARED_APP:
        return None
    return settings


class SlackRoute(ABC):
    """Answer one Slack route's requests, and send its message updates a few at a time."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._follow_ups = asyncio.Semaphore(FOLLOW_UP_CONCURRENCY)

    @abstractmethod
    async def handle(self, body: bytes, headers: Mapping[str, str]) -> SlackReply: ...

    async def send_follow_up(self, follow_up: FollowUp) -> None:
        async with self._follow_ups:
            try:
                await asyncio.to_thread(post_follow_up, follow_up)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "the Slack message update of %s failed with %s",
                    self._label,
                    type(exc).__name__,
                )


class SlackInteractivity(SlackRoute):
    """Verify, decode, and dispatch one tenant's Slack interactivity requests."""

    def __init__(
        self,
        *,
        tenant: str,
        secret: str,
        slack: SlackSettingsSource,
        dispatcher: SlackDispatcher,
    ) -> None:
        super().__init__(f"tenant {tenant}")
        self._tenant = tenant
        self._secret = secret
        self._slack = slack
        self._dispatcher = dispatcher

    def _served_settings(self) -> SlackSettings | None:
        """Return the tenant's Slack settings while the webhook serves its Slack route."""
        settings = self._slack(self._tenant)
        if (
            settings is None
            or settings.transport != WEBHOOK_TRANSPORT
            or settings.app == SHARED_APP
        ):
            return None
        return settings

    async def handle(self, body: bytes, headers: Mapping[str, str]) -> SlackReply:
        settings = self._served_settings()
        if settings is None:
            return SlackReply(404, "not found")
        payload = verified_payload(self._secret, body, headers)
        if isinstance(payload, SlackReply):
            return payload
        if payload.get("api_app_id") != settings.app_id:
            return SlackReply(401, "unknown app")
        if payload.get("type") != BLOCK_ACTIONS:
            return SlackReply(200)
        result = await self._dispatcher.dispatch(click_from_payload(payload))
        response_url = str(payload.get("response_url") or "")
        if not response_url_allowed(response_url, settings):
            return SlackReply(200)
        return SlackReply(200, follow_up=FollowUp(response_url, follow_up_message(result)))


class SharedSlackInteractivity(SlackRoute):
    """Verify the shared Slack app's requests and dispatch each click under its channel's tenant.

    While the snapshot holds no shared app the route answers 503, because Slack retries a 5xx and
    not a 404. A click from a channel the index does not route goes to ``retained_holder``, the
    tenant that left the channel and still holds its claim. ``decide`` holds every check after the
    signature, which the Socket Mode consumer also calls for a payload its connection delivered.
    """

    def __init__(
        self,
        *,
        route: str,
        secret: str,
        shared_app: SharedAppSource,
        slack: SlackSettingsSource,
        channel_tenant: ChannelTenantSource,
        dispatchers: Mapping[str, SlackDispatcher],
        records: DecisionRecords,
        retained_holder: RetainedHolderSource,
    ) -> None:
        super().__init__("the shared Slack app")
        self._route = route
        self._secret = secret
        self._shared_app = shared_app
        self._slack = slack
        self._channel_tenant = channel_tenant
        self._dispatchers = dispatchers
        self._records = records
        self._retained_holder = retained_holder

    async def handle(self, body: bytes, headers: Mapping[str, str]) -> SlackReply:
        if self._shared_app() is None:
            return SlackReply(503, "the shared Slack app is not configured")
        payload = verified_payload(self._secret, body, headers)
        if isinstance(payload, SlackReply):
            return payload
        return await self.decide(payload)

    async def decide(self, payload: dict[str, Any]) -> SlackReply:
        """Decide an authenticated payload of the shared app under the tenant of its channel."""
        shared_app = self._shared_app()
        if shared_app is None:
            return SlackReply(503, "the shared Slack app is not configured")
        if payload.get("api_app_id") != shared_app.app_id:
            return SlackReply(401, "unknown app")
        if payload.get("type") != BLOCK_ACTIONS:
            return SlackReply(200)
        click = click_from_payload(payload)
        tenant = self._channel_tenant(click.channel)
        retained = None
        if tenant is None and click.channel:
            tenant = await self._retained_holder(click.channel)
            retained = click.channel
        dispatcher = None if tenant is None else self._dispatchers.get(tenant)
        settings = None
        if tenant is None or dispatcher is None:
            result = await refuse_unrouted(click, self._records, route=self._route)
        else:
            result = await dispatcher.dispatch(click, retained_channel=retained)
            settings = shared_app_settings(self._slack, tenant)
        response_url = str(payload.get("response_url") or "")
        allowed = (
            response_url_allowed(response_url, settings)
            if settings is not None
            else hooks_url_allowed(response_url, host_of(shared_app.api_base_url))
        )
        if not allowed:
            return SlackReply(200)
        return SlackReply(200, follow_up=FollowUp(response_url, follow_up_message(result)))
