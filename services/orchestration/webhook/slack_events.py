"""Accept a tenant's Slack interactivity at /webhook/slack/<tenant> and answer through response_url.

The request is refused with 401 when its X-Slack-Request-Timestamp is missing, is not a number, or
is more than five minutes from the server clock, and when its X-Slack-Signature is not the v0
HMAC-SHA256 of the timestamp and the raw body under the tenant's signing secret. Only a verified
body is decoded. A click is decided by the tenant's dispatcher before the response, and the message
is updated afterwards through the click's response_url, which is called only when it belongs to the
Slack hooks origin of the tenant's API host, without following redirects. A tenant is served only
while its Slack settings source returns settings whose transport is webhook.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import httpx

from services.integration.slack.dispatcher import (
    DispatchResult,
    SlackDispatcher,
    click_from_payload,
)
from services.integration.slack.messages import ephemeral_reply
from services.integration.slack.policy import response_url_allowed
from services.orchestration.escalation import WEBHOOK_TRANSPORT, SlackSettings

log = logging.getLogger(__name__)

TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SIGNATURE_HEADER = "X-Slack-Signature"
MAX_CLOCK_SKEW_SECONDS = 300
FOLLOW_UP_TIMEOUT_SECONDS = 5.0
FOLLOW_UP_CONCURRENCY = 4
BLOCK_ACTIONS = "block_actions"

SlackSettingsSource = Callable[[str], SlackSettings | None]


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


class SlackInteractivity:
    """Verify, decode, and dispatch one tenant's Slack interactivity requests."""

    def __init__(
        self,
        *,
        tenant: str,
        secret: str,
        slack: SlackSettingsSource,
        dispatcher: SlackDispatcher,
    ) -> None:
        self._tenant = tenant
        self._secret = secret
        self._slack = slack
        self._dispatcher = dispatcher
        self._follow_ups = asyncio.Semaphore(FOLLOW_UP_CONCURRENCY)

    def _served_settings(self) -> SlackSettings | None:
        """Return the tenant's Slack settings while the webhook serves its Slack route."""
        settings = self._slack(self._tenant)
        if settings is None or settings.transport != WEBHOOK_TRANSPORT:
            return None
        return settings

    async def handle(self, body: bytes, headers: Mapping[str, str]) -> SlackReply:
        settings = self._served_settings()
        if settings is None:
            return SlackReply(404, "not found")
        timestamp = headers.get(TIMESTAMP_HEADER)
        if timestamp is None or not timestamp_fresh(timestamp, time.time()):
            return SlackReply(401, "invalid timestamp")
        if not signature_valid(self._secret, timestamp, body, headers.get(SIGNATURE_HEADER)):
            return SlackReply(401, "invalid signature")
        payload = decode_payload(body)
        if payload is None:
            return SlackReply(400, "invalid payload")
        if payload.get("api_app_id") != settings.app_id:
            return SlackReply(401, "unknown app")
        if payload.get("type") != BLOCK_ACTIONS:
            return SlackReply(200)
        result = await self._dispatcher.dispatch(click_from_payload(payload))
        response_url = str(payload.get("response_url") or "")
        if not response_url_allowed(response_url, settings):
            return SlackReply(200)
        return SlackReply(200, follow_up=FollowUp(response_url, follow_up_message(result)))

    async def send_follow_up(self, follow_up: FollowUp) -> None:
        async with self._follow_ups:
            try:
                await asyncio.to_thread(post_follow_up, follow_up)
            except Exception as exc:  # noqa: BLE001
                # The exception text of httpx names the response_url, which is never logged.
                log.warning(
                    "the Slack message update of tenant %s failed with %s",
                    self._tenant,
                    type(exc).__name__,
                )
