"""HTTP entrypoint of the Slack posting service.

POST /v1/messages posts one alert of a tenant on the shared Slack app to the channel that the
tenant's latest escalation.yaml names. The tenant comes from a TokenReview of the bearer token and
never from the request, and the body may carry only text, blocks, thread_ts, and audience, so a
caller cannot name a channel, a username, or an icon. thread_ts posts the message as a reply in
the thread of that message. audience admin posts to the admin Slack channel that
platform-escalation.yaml names in place of the tenant's channel, for an administrator alert of
any served tenant. The platform caller, the inbound webhook or the deployment-validate Job, posts
only with audience admin.

POST /v1/messages/update edits a message with chat.update, and its body carries ts, text, blocks,
audience, and owner_token. A tenant edits only messages in its own channel.

The answer to a new admin post carries owner_token, an HMAC-SHA256 over the admin channel, the
post's ts, and the caller, the tenant or the platform caller. An edit of an admin post, or a reply
in its thread, must present that token, so a caller edits only the admin posts it posted, and a
tenant never edits a post of the platform caller or of another tenant. The key is derived from the
shared bot token, so the tokens hold across a restart of the service and lapse when the bot token
rotates or platform-escalation.yaml names another admin channel.

The deadline header bounds the Slack call, up to a server maximum.

Slack rates chat.postMessage at one message per second per channel, so each channel, a tenant's
channel or the admin channel, holds a token bucket of POST_BURST posts and thread replies refilled
at POSTS_PER_SECOND, 2 posts at 0.5 per second. The admin channel's bucket is shared by every caller
that posts to it. Slack rates chat.update at Tier 3, 50 or more calls per minute for the whole
shared app in a workspace, so every tenant's edits draw from one app-wide bucket of EDIT_BURST edits
refilled at EDITS_PER_SECOND, 4 edits at 40 per minute. The cap of one held call per tenant keeps
one tenant from holding the edit bucket ahead of another.

A call that finds its bucket without a token is held, in order behind the calls its bucket already
holds, and sent as soon as the bucket refills. A call is held only while MIN_DEADLINE_SECONDS of its
deadline would remain for Slack. A call that would still be held then is refused with 429 and
Retry-After. As a fairness cap, a tenant holds at most MAX_HELD_PER_TENANT call, so on the shared
admin channel another tenant waits behind at most that many calls of each tenant. A tenant also has
at most MAX_IN_FLIGHT calls held or at Slack. When Slack answers 429, every call pauses for every
tenant for the Retry-After seconds, because the shared app has one rate budget. The buckets, the
caps, and the pause hold only while one process serves every post.

GET /v1/approver-groups/<group_id> answers the user IDs of one Slack user group, read with
usergroups.users.list and the shared bot token, for the worker the TokenReview names. It answers
only for a group that the tenant's latest escalation.yaml names under a decision kind, from the
snapshot the post path reads, and refuses any other group with 403. The request carries only the
group ID, and the read passes team_id only when the group's entry in escalation.yaml sets it. Each
tenant holds a token bucket of MAX_APPROVER_GROUPS group reads, refilled over the worker's refresh
interval, so a tenant within the group limit passes the bucket in every refresh cycle. The platform
caller reads only the groups the allowlist of platform-escalation.yaml names, and a tenant worker
reads none of them. A 429 from
Slack pauses group reads for every tenant, because usergroups.users.list has a rate budget apart
from chat.postMessage.

/health answers while the process serves. /ready answers 200 only while the snapshot holds a shared
app and auth.test has accepted the shared bot token for that app, naming its organization as the
enterprise, or as the team when there is no enterprise. Until then the service repeats auth.test
every READINESS_RETRY_SECONDS and refuses every post. A failed first refresh of the snapshot keeps
the process serving /health, and the snapshot's next refresh retries it.

The Slack call gets the caller's deadline less the time the TokenReview took, and a post with less
than MIN_DEADLINE_SECONDS left is refused before it reaches Slack. The service answers 504 once the
deadline passes, and a post Slack received before then can still appear in the channel.

The service serves the certificate and key that SERVER_TLS_CERTIFICATE_FILE and SERVER_TLS_KEY_FILE
name, issued by the CA each worker trusts through SLACK_POSTING_CA_FILE. It logs
slack_poster_leaf_expiring at start and once a day while the certificate it loaded is within
LEAF_WARNING of its expiry, and slack_poster_leaf_renewed while the file holds a renewed one that
only a restart serves.

No log event carries the request body, which holds action tokens, or the Authorization header.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import datetime
import hashlib
import hmac
import json
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

import structlog
from scripts.config import Settings, configured_log_level, get_settings
from slack_sdk.errors import SlackApiError, SlackClientError

from services.http_front_door import REQUEST_TIMEOUT_SECONDS, RequestRejected, read_bounded_body
from services.integration.credentials import ConfigError
from services.integration.slack.approver_groups import GROUP_REFRESH_SECONDS, group_read_arguments
from services.integration.slack.client import tenant_web_client
from services.integration.slack.shared_app import SharedSlackApp
from services.integration.slack.tenant_settings import TenantSlackSettings
from services.integration.tenant_config import BundleSource, tenant_source
from services.orchestration.escalation import (
    MAX_APPROVER_GROUPS,
    SHARED_APP,
    SHARED_APP_CHANNEL,
    Approvers,
    SlackGroup,
    SlackSettings,
    shared_app_tenants,
)
from services.orchestration.platform_escalation import (
    AdminDestination,
    platform_escalation_preflight,
    process_admin_destination,
)
from services.repository_intelligence.tenants import served_tenants
from services.slack_poster import (
    ADMIN_AUDIENCE,
    DEADLINE_HEADER,
    GROUPS_PATH,
    MESSAGES_PATH,
    MIN_DEADLINE_SECONDS,
    OWNER_PROOF_FIELD,
    PORT,
    UPDATE_PATH,
)
from services.slack_poster.auth import (
    PLATFORM_CALLER,
    IdentityRefused,
    KubernetesTokenReview,
    ReviewUnavailable,
    WorkerIdentity,
)

log = structlog.get_logger("slack-poster")

SERVICE = "slack-poster"
MAX_DEADLINE_SECONDS = 5.0
POSTS_PER_SECOND = 0.5
POST_BURST = 2
EDITS_PER_SECOND = 40 / 60
EDIT_BURST = 4
SHARED_APP_EDITS = "shared-app"
MAX_IN_FLIGHT = 2
MAX_HELD_PER_TENANT = 1
SLACK_CALL_WORKERS = 16
DEFAULT_PAUSE_SECONDS = 60.0
READINESS_RETRY_SECONDS = 30.0
LEAF_WARNING = datetime.timedelta(days=30)
LEAF_CHECK_SECONDS = 86400.0
BODY_FIELDS = frozenset({"text", "blocks", "thread_ts", "audience", OWNER_PROOF_FIELD})
UPDATE_FIELDS = frozenset({"text", "blocks", "ts", "audience", OWNER_PROOF_FIELD})
MESSAGE_TS = re.compile(r"^[0-9]{1,12}\.[0-9]{1,8}$")
OWNER_TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{1,64}")
OWNER_KEY_LABEL = b"agentic-dev-platform slack-poster admin post owner"
RATE_LIMITED = "ratelimited"
GROUP_READ_BURST = MAX_APPROVER_GROUPS
GROUP_READ_REFILL_SECONDS = float(GROUP_REFRESH_SECONDS)
GROUP_PATH_ID = re.compile(r"[A-Za-z0-9]{1,32}")

SlackClientFactory = Callable[[SharedSlackApp, int], Any]


class SlackSnapshot(Protocol):
    def slack(self, tenant: str) -> SlackSettings | None: ...

    def channel_tenant(self, channel: str) -> str | None: ...

    def shared_app(self) -> SharedSlackApp | None: ...


class TenantIdentity(Protocol):
    def tenant(self, token: str) -> str: ...


@dataclass(frozen=True)
class Reply:
    status: int
    body: Mapping[str, Any]

    @property
    def retry_after(self) -> float | None:
        return self.body.get("retry_after")


class _Refused(Exception):
    def __init__(
        self, status: int, reason: str, *, error: str = "", retry_after: float | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        body: dict[str, Any] = {"error": error or reason}
        if retry_after is not None:
            body["retry_after"] = round(retry_after, 3)
        self.reply = Reply(status, body)


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass(frozen=True)
class _Rate:
    burst: float
    per_second: float
    reason: str
    error: str


POST_RATE = _Rate(
    POST_BURST, POSTS_PER_SECOND, "the channel posts faster than its rate", "channel_rate_limited"
)
EDIT_RATE = _Rate(
    EDIT_BURST, EDITS_PER_SECOND, "the shared app edits faster than its rate", "edit_rate_limited"
)


@dataclass
class _HeldBucket:
    rate: _Rate
    tokens: float
    updated: float
    held: deque[object] = field(default_factory=deque)

    def refill(self, now: float) -> None:
        self.tokens = min(
            self.rate.burst, self.tokens + (now - self.updated) * self.rate.per_second
        )
        self.updated = now

    def wait(self, position: int) -> float:
        """Return the seconds until the call at ``position`` of the held calls gets a token."""
        return max(0.0, (position + 1 - self.tokens) / self.rate.per_second)

    def refusal(self, position: int) -> _Refused:
        return _Refused(
            429, self.rate.reason, error=self.rate.error, retry_after=self.wait(position)
        )


@dataclass(frozen=True)
class _Message:
    text: str
    blocks: list[Any] | None
    thread_ts: str = ""
    audience: str = ""
    ts: str = ""
    owner_token: str = ""

    @property
    def admin(self) -> bool:
        return self.audience == ADMIN_AUDIENCE


def _find_group(allowlist: Mapping[str, Approvers], group_id: str) -> SlackGroup | None:
    for approvers in allowlist.values():
        for group in approvers.groups:
            if group.group_id == group_id:
                return group
    return None


class PostingService:
    """Post the alerts of tenants on the shared Slack app with the shared bot token."""

    def __init__(
        self,
        *,
        identity: TenantIdentity,
        snapshot: SlackSnapshot,
        slack_client: SlackClientFactory,
        owner_key: bytes,
        clock: Callable[[], float] = time.monotonic,
        admin: Callable[[], AdminDestination | None] = lambda: None,
        hold: Callable[[threading.Condition, float], object] = threading.Condition.wait,
    ) -> None:
        self._identity = identity
        self._admin = admin
        self._owner_key = owner_key
        self._snapshot = snapshot
        self._slack_client = slack_client
        self._clock = clock
        self._hold = hold
        self._lock = threading.Lock()
        self._refilled = threading.Condition(self._lock)
        self._buckets: dict[tuple[_Rate, str], _HeldBucket] = {}
        self._in_flight: dict[str, int] = {}
        self._held: dict[str, int] = {}
        self._paused_until = 0.0
        self._group_buckets: dict[str, _Bucket] = {}
        self._group_reads_paused_until = 0.0
        self._checked: SharedSlackApp | None = None
        self._calls = concurrent.futures.ThreadPoolExecutor(
            max_workers=SLACK_CALL_WORKERS, thread_name_prefix="slack-call"
        )

    @property
    def ready(self) -> bool:
        """Return whether auth.test accepted the token for the shared app the snapshot holds."""
        return self._ready_app() is not None

    def _ready_app(self) -> SharedSlackApp | None:
        shared_app = self._snapshot.shared_app()
        return shared_app if shared_app is not None and shared_app == self._checked else None

    def check_slack(self) -> None:
        """Run auth.test, and mark the service ready when it names the shared app's organization."""
        self._checked = None
        shared_app = self._snapshot.shared_app()
        if shared_app is None:
            log.error("slack_auth_test_skipped", reason="the snapshot holds no shared app")
            return
        try:
            response = self._slack_client(shared_app, int(MAX_DEADLINE_SECONDS)).auth_test()
        except SlackApiError as exc:
            log.error("slack_auth_test_failed", reason=str(exc.response.get("error")))
            return
        except (SlackClientError, OSError) as exc:
            log.error("slack_auth_test_failed", reason=type(exc).__name__)
            return
        organization = response.get("enterprise_id") or response.get("team_id")
        if organization != shared_app.organization_id:
            log.error(
                "slack_auth_test_failed",
                reason="the token belongs to another organization",
                organization=organization,
                expected=shared_app.organization_id,
            )
            return
        log.info("slack_auth_test_passed", organization=organization)
        self._checked = shared_app

    def post(
        self, *, authorization: str | None, deadline: str | None, body: bytes, update: bool = False
    ) -> Reply:
        received = self._clock()
        tenant: str | None = None
        channel: str | None = None
        try:
            shared_app = self._ready_app()
            if shared_app is None:
                raise _Refused(503, "the service is not ready", error="not_ready")
            tenant = self._tenant(authorization)
            if tenant != PLATFORM_CALLER:
                channel = self._channel(tenant)
            message = _message(body, UPDATE_FIELDS if update else BODY_FIELDS)
            if message.admin:
                channel = self._admin_channel(tenant, message)
            elif channel is None:
                raise _Refused(
                    403,
                    "the platform caller posts only to the admin channel",
                    error="platform_caller_not_admin",
                )
            seconds = _deadline_seconds(deadline)
            _remaining(seconds, self._clock() - received)
            rate, key = (EDIT_RATE, SHARED_APP_EDITS) if message.ts else (POST_RATE, channel)
            admitted = self._admit(tenant, rate, key, received + seconds - MIN_DEADLINE_SECONDS)
            timeout = int(seconds - (admitted - received))
        except _Refused as refusal:
            log.warning(
                "slack_post_refused",
                tenant=tenant,
                channel=channel,
                reason=refusal.reason,
                status=refusal.reply.status,
            )
            return refusal.reply
        try:
            return self._send(shared_app, tenant, channel, message, timeout)
        finally:
            with self._lock:
                self._in_flight[tenant] -= 1

    def _admin_channel(self, caller: str, message: _Message) -> str:
        destination = self._admin()
        if destination is None or not destination.slack:
            raise _Refused(
                403, "platform-escalation.yaml names no admin channel", error="no_admin_channel"
            )
        channel = destination.slack_channel
        target = message.ts or message.thread_ts
        if target and not hmac.compare_digest(
            message.owner_token.encode(), self.owner_token(channel, target, caller).encode()
        ):
            raise _Refused(
                403, "the admin post is not one this caller posted", error="post_not_owned"
            )
        return channel

    def owner_token(self, channel: str, ts: str, caller: str) -> str:
        """Return the owner token of the admin post ``ts`` in ``channel`` that ``caller`` made."""
        digest = hmac.new(
            self._owner_key, f"{channel}\n{ts}\n{caller}".encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _tenant(self, authorization: str | None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise _Refused(401, "a bearer token is required", error="unauthenticated")
        try:
            return self._identity.tenant(token)
        except IdentityRefused as exc:
            raise _Refused(401, exc.reason, error="unauthenticated") from exc
        except ReviewUnavailable as exc:
            raise _Refused(503, str(exc), error="token_review_unavailable") from exc

    def _channel(self, tenant: str) -> str:
        slack = self._snapshot.slack(tenant)
        if slack is None or slack.app != SHARED_APP:
            raise _Refused(403, "the tenant is not on the shared app", error="not_shared_app")
        channel = slack.channel.strip()
        if not SHARED_APP_CHANNEL.fullmatch(channel):
            raise _Refused(
                403, "the tenant's channel is not a Slack channel ID", error="channel_refused"
            )
        if self._snapshot.channel_tenant(channel) != tenant:
            raise _Refused(403, "the tenant does not hold its channel", error="channel_not_held")
        return channel

    def read_group(
        self, *, authorization: str | None, deadline: str | None, group_id: str
    ) -> Reply:
        """Answer the members of a group the caller's tenant names, as {"members": [...]}."""
        received = self._clock()
        tenant: str | None = None
        try:
            shared_app = self._ready_app()
            if shared_app is None:
                raise _Refused(503, "the service is not ready", error="not_ready")
            tenant = self._tenant(authorization)
            if tenant == PLATFORM_CALLER:
                group = self._admin_group(group_id)
            else:
                group = self._named_group(tenant, group_id)
            timeout = _remaining(_deadline_seconds(deadline), self._clock() - received)
            self._admit_group_read(tenant)
        except _Refused as refusal:
            log.warning(
                "slack_group_read_refused",
                tenant=tenant,
                group=group_id,
                reason=refusal.reason,
                status=refusal.reply.status,
            )
            return refusal.reply
        return self._read_members(shared_app, tenant, group, timeout)

    def _named_group(self, tenant: str, group_id: str) -> SlackGroup:
        slack = self._snapshot.slack(tenant)
        if slack is None or slack.app != SHARED_APP:
            raise _Refused(403, "the tenant is not on the shared app", error="not_shared_app")
        group = _find_group(slack.allowlist, group_id)
        if group is None:
            raise _Refused(
                403, "the tenant's escalation.yaml does not name the group", error="group_not_named"
            )
        return group

    def _admin_group(self, group_id: str) -> SlackGroup:
        destination = self._admin()
        group = _find_group(destination.allowlist if destination else {}, group_id)
        if group is None:
            raise _Refused(
                403, "platform-escalation.yaml does not name the group", error="group_not_named"
            )
        return group

    def _admit_group_read(self, tenant: str) -> None:
        with self._lock:
            now = self._clock()
            if self._group_reads_paused_until > now:
                raise _Refused(
                    429,
                    "Slack rate limited the shared app's group reads",
                    error=RATE_LIMITED,
                    retry_after=self._group_reads_paused_until - now,
                )
            rate = GROUP_READ_BURST / GROUP_READ_REFILL_SECONDS
            bucket = self._group_buckets.setdefault(tenant, _Bucket(GROUP_READ_BURST, now))
            bucket.tokens = min(GROUP_READ_BURST, bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
            if bucket.tokens < 1:
                raise _Refused(
                    429,
                    "the tenant reads groups faster than its rate",
                    error="tenant_rate_limited",
                    retry_after=(1 - bucket.tokens) / rate,
                )
            bucket.tokens -= 1

    def _read_members(
        self, shared_app: SharedSlackApp, tenant: str, group: SlackGroup, timeout: int
    ) -> Reply:
        call = self._calls.submit(
            self._slack_client(shared_app, timeout).usergroups_users_list,
            **group_read_arguments(group),
        )
        try:
            response = call.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning(
                "slack_group_read_failed",
                tenant=tenant,
                group=group.group_id,
                reason="deadline_exceeded",
            )
            return Reply(504, {"error": "deadline_exceeded"})
        except SlackApiError as exc:
            error = str(exc.response.get("error") or "unknown_error")
            log.warning(
                "slack_group_read_failed", tenant=tenant, group=group.group_id, reason=error
            )
            if exc.response.status_code == 429:
                seconds = _retry_after(exc.response.headers)
                with self._lock:
                    self._group_reads_paused_until = max(
                        self._group_reads_paused_until, self._clock() + seconds
                    )
                return Reply(429, {"error": RATE_LIMITED, "retry_after": seconds})
            return Reply(502, {"error": error})
        except (SlackClientError, OSError) as exc:
            log.warning(
                "slack_group_read_failed",
                tenant=tenant,
                group=group.group_id,
                reason=type(exc).__name__,
            )
            return Reply(504, {"error": "slack_unreachable"})
        members = response.get("users")
        if not isinstance(members, list) or not all(isinstance(user, str) for user in members):
            log.warning(
                "slack_group_read_failed",
                tenant=tenant,
                group=group.group_id,
                reason="no_users",
            )
            return Reply(502, {"error": "no_users"})
        log.info("slack_group_read", tenant=tenant, group=group.group_id, members=len(members))
        return Reply(200, {"members": members})

    def _refuse_while_paused(self, now: float) -> None:
        if self._paused_until > now:
            raise _Refused(
                429,
                "Slack rate limited the shared app",
                error=RATE_LIMITED,
                retry_after=self._paused_until - now,
            )

    def _admit(self, tenant: str, rate: _Rate, key: str, deadline: float) -> float:
        with self._lock:
            now = self._clock()
            self._refuse_while_paused(now)
            if self._in_flight.get(tenant, 0) + self._held.get(tenant, 0) >= MAX_IN_FLIGHT:
                raise _Refused(
                    429, "the tenant has too many posts in flight", error="too_many_in_flight"
                )
            bucket = self._buckets.setdefault((rate, key), _HeldBucket(rate, rate.burst, now))
            bucket.refill(now)
            if not bucket.held and bucket.tokens >= 1:
                bucket.tokens -= 1
            else:
                if now + bucket.wait(len(bucket.held)) > deadline:
                    raise bucket.refusal(len(bucket.held))
                if self._held.get(tenant, 0) >= MAX_HELD_PER_TENANT:
                    raise _Refused(
                        429,
                        "the tenant already holds a call for a rate token",
                        error="too_many_held",
                        retry_after=bucket.wait(len(bucket.held)),
                    )
                now = self._hold_for_token(tenant, bucket, deadline)
            self._in_flight[tenant] = self._in_flight.get(tenant, 0) + 1
            return now

    def _hold_for_token(self, tenant: str, bucket: _HeldBucket, deadline: float) -> float:
        call = object()
        bucket.held.append(call)
        self._held[tenant] = self._held.get(tenant, 0) + 1
        try:
            while True:
                now = self._clock()
                self._refuse_while_paused(now)
                position = bucket.held.index(call)
                if now > deadline:
                    raise bucket.refusal(position)
                bucket.refill(now)
                if position == 0 and bucket.tokens >= 1:
                    bucket.tokens -= 1
                    return now
                seconds = deadline - now
                if position == 0:
                    seconds = min(seconds, bucket.wait(0))
                self._hold(self._refilled, seconds)
        finally:
            bucket.held.remove(call)
            self._held[tenant] -= 1
            self._refilled.notify_all()

    def _send(
        self, shared_app: SharedSlackApp, tenant: str, channel: str, message: _Message, timeout: int
    ) -> Reply:
        client = self._slack_client(shared_app, timeout)
        if message.ts:
            call = self._calls.submit(
                client.chat_update,
                channel=channel,
                ts=message.ts,
                text=message.text,
                blocks=message.blocks,
            )
        else:
            arguments: dict[str, Any] = {}
            if message.thread_ts:
                arguments["thread_ts"] = message.thread_ts
            call = self._calls.submit(
                client.chat_postMessage,
                channel=channel,
                text=message.text,
                blocks=message.blocks,
                unfurl_links=False,
                unfurl_media=False,
                **arguments,
            )
        try:
            response = call.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning(
                "slack_post_failed", tenant=tenant, channel=channel, reason="deadline_exceeded"
            )
            return Reply(504, {"error": "deadline_exceeded"})
        except SlackApiError as exc:
            error = str(exc.response.get("error") or "unknown_error")
            log.warning("slack_post_failed", tenant=tenant, channel=channel, reason=error)
            if exc.response.status_code == 429:
                return self._pause(_retry_after(exc.response.headers))
            return Reply(502, {"error": error})
        except (SlackClientError, OSError) as exc:
            log.warning(
                "slack_post_failed", tenant=tenant, channel=channel, reason=type(exc).__name__
            )
            return Reply(504, {"error": "slack_unreachable"})
        ts = response.get("ts")
        answer: dict[str, Any] = {"ts": ts}
        if message.admin and not message.ts and not message.thread_ts and isinstance(ts, str):
            answer[OWNER_PROOF_FIELD] = self.owner_token(channel, ts, tenant)
        log.info("slack_post_delivered", tenant=tenant, channel=channel, ts=ts)
        return Reply(200, answer)

    def _pause(self, seconds: float) -> Reply:
        with self._lock:
            self._paused_until = max(self._paused_until, self._clock() + seconds)
        return Reply(429, {"error": RATE_LIMITED, "retry_after": seconds})


def _message(body: bytes, fields: frozenset[str] = BODY_FIELDS) -> _Message:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError, UnicodeDecodeError:
        raise _Refused(400, "the body is not JSON", error="invalid_body") from None
    if not isinstance(payload, dict):
        raise _Refused(400, "the body is not a JSON object", error="invalid_body")
    if not fields.issuperset(payload):
        raise _Refused(
            400,
            f"the body names fields other than {', '.join(sorted(fields))}",
            error="invalid_body",
        )
    text, blocks = payload.get("text"), payload.get("blocks")
    if not isinstance(text, str) or not text.strip():
        raise _Refused(400, "the body has no text", error="invalid_body")
    if blocks is not None and not isinstance(blocks, list):
        raise _Refused(400, "the body's blocks are not a list", error="invalid_body")
    stamps = {key: payload.get(key) or "" for key in ("thread_ts", "ts")}
    for key, value in stamps.items():
        if value and not (isinstance(value, str) and MESSAGE_TS.match(value)):
            raise _Refused(
                400, f"the body's {key} is not a message timestamp", error="invalid_body"
            )
    if "ts" in fields and not stamps["ts"]:
        raise _Refused(400, "the update names no ts", error="invalid_body")
    audience = payload.get("audience") or ""
    if audience not in ("", ADMIN_AUDIENCE):
        raise _Refused(400, f"the body's audience is not {ADMIN_AUDIENCE}", error="invalid_body")
    owner_token = payload.get(OWNER_PROOF_FIELD) or ""
    if owner_token and not (
        isinstance(owner_token, str) and OWNER_TOKEN_FORMAT.fullmatch(owner_token)
    ):
        raise _Refused(400, f"the body's {OWNER_PROOF_FIELD} is malformed", error="invalid_body")
    return _Message(
        text, blocks, str(stamps["thread_ts"]), audience, str(stamps["ts"]), owner_token
    )


def _deadline_seconds(deadline: str | None) -> float:
    if deadline is None:
        return MAX_DEADLINE_SECONDS
    try:
        seconds = float(deadline)
    except ValueError:
        raise _Refused(
            400, "the deadline header is not a number", error="invalid_deadline"
        ) from None
    if not math.isfinite(seconds) or seconds < MIN_DEADLINE_SECONDS:
        raise _Refused(400, "the deadline leaves too little time to post", error="invalid_deadline")
    return min(seconds, MAX_DEADLINE_SECONDS)


def _remaining(seconds: float, elapsed: float) -> int:
    left = seconds - elapsed
    if left < MIN_DEADLINE_SECONDS:
        raise _Refused(504, "the deadline passed before the post", error="deadline_exceeded")
    return int(left)


def _retry_after(headers: Mapping[str, Any]) -> float:
    try:
        seconds = float(headers.get("Retry-After") or headers.get("retry-after") or "")
    except ValueError:
        return DEFAULT_PAUSE_SECONDS
    return seconds if math.isfinite(seconds) and seconds > 0 else DEFAULT_PAUSE_SECONDS


class TenantBundleSources:
    """The bundle source of each served tenant, for the tenant Slack settings snapshot."""

    def __init__(self, settings: Settings, tenants: Sequence[str]) -> None:
        self._settings = settings
        self._tenants = list(tenants)
        self._sources: dict[str, BundleSource] = {}

    def tenants(self) -> list[str]:
        return list(self._tenants)

    def source(self, tenant: str) -> BundleSource:
        source = self._sources.get(tenant)
        if source is None:
            source = self._sources[tenant] = tenant_source(self._settings, tenant)
        return source


def tenant_slack_settings(
    settings: Settings, tenants: Sequence[str] | None = None
) -> TenantSlackSettings:
    served = served_tenants(settings) if tenants is None else tenants
    return TenantSlackSettings(settings, TenantBundleSources(settings, served))


def owner_key(bot_token: str) -> bytes:
    """Return the key of the admin post owner tokens, derived from the shared bot token."""
    return hmac.new(bot_token.encode(), OWNER_KEY_LABEL, hashlib.sha256).digest()


class SlackClients:
    """One WebClient of the shared bot token per API host and timeout."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._clients: dict[tuple[str, int], Any] = {}
        self._lock = threading.Lock()

    def __call__(self, shared_app: SharedSlackApp, timeout: int) -> Any:
        key = (shared_app.api_base_url, timeout)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._clients[key] = tenant_web_client(
                    self._token, shared_app.api_base_url, timeout=timeout, retry_handlers=[]
                )
            return client


def build_handler(service: PostingService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SECONDS

        def do_GET(self) -> None:
            if self.path in ("/health", "/healthz", "/"):
                self._respond(Reply(200, {"status": "ok", "service": SERVICE}))
            elif self.path == "/ready":
                ready = service.ready
                self._respond(Reply(200 if ready else 503, {"ready": ready, "service": SERVICE}))
            elif self.path.startswith(GROUPS_PATH) and GROUP_PATH_ID.fullmatch(
                group_id := self.path.removeprefix(GROUPS_PATH)
            ):
                self._respond(
                    service.read_group(
                        authorization=self.headers.get("Authorization"),
                        deadline=self.headers.get(DEADLINE_HEADER),
                        group_id=group_id,
                    )
                )
            else:
                self._respond(Reply(404, {"error": "not found"}))

        def do_POST(self) -> None:
            if self.path not in (MESSAGES_PATH, UPDATE_PATH):
                self._respond(Reply(404, {"error": "not found"}))
                return
            try:
                body = read_bounded_body(
                    cast(BinaryIO, self.rfile), cast(Mapping[str, str], self.headers)
                )
            except RequestRejected as exc:
                self.close_connection = True
                self._respond(Reply(exc.status, {"error": exc.message}))
                return
            self._respond(
                service.post(
                    authorization=self.headers.get("Authorization"),
                    deadline=self.headers.get(DEADLINE_HEADER),
                    body=body,
                    update=self.path == UPDATE_PATH,
                )
            )

        def _respond(self, reply: Reply) -> None:
            encoded = json.dumps(reply.body).encode()
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if reply.retry_after is not None:
                self.send_header("Retry-After", str(math.ceil(reply.retry_after)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


async def await_slack(service: PostingService, interval: float = READINESS_RETRY_SECONDS) -> None:
    """Repeat auth.test until the service is ready, so a Slack outage at startup heals."""
    while True:
        await asyncio.to_thread(service.check_slack)
        if service.ready:
            return
        await asyncio.sleep(interval)


def check_leaf(
    served: datetime.datetime, certificate: Path, now: datetime.datetime | None = None
) -> None:
    """Log slack_poster_leaf_expiring when the served certificate, which expires at ``served``,
    expires within LEAF_WARNING, and slack_poster_leaf_renewed when the file holds another."""
    from services.server_tls import expires_at

    left = served - (now or datetime.datetime.now(datetime.UTC))
    if left <= LEAF_WARNING:
        log.error(
            "slack_poster_leaf_expiring",
            expires=served.isoformat(),
            days_left=left.days,
            remedy="inv slack-poster-certs issue and put in AWS, or inv k8s-reload locally",
        )
    try:
        on_disk = expires_at(certificate)
    except (OSError, ValueError) as exc:
        log.error("slack_poster_leaf_unreadable", path=str(certificate), reason=type(exc).__name__)
        return
    if on_disk != served:
        log.warning(
            "slack_poster_leaf_renewed",
            served_expires=served.isoformat(),
            file_expires=on_disk.isoformat(),
            remedy="restart slack-poster to serve the certificate in the file",
        )


async def watch_leaf(
    served: datetime.datetime, certificate: Path, interval: float = LEAF_CHECK_SECONDS
) -> None:
    while True:
        await asyncio.to_thread(check_leaf, served, certificate)
        await asyncio.sleep(interval)


async def watch_slack(service: PostingService, interval: float = READINESS_RETRY_SECONDS) -> None:
    """Keep the service ready, repeating auth.test whenever the snapshot's shared app changes."""
    while True:
        try:
            if not service.ready:
                await await_slack(service, interval)
        except Exception as exc:  # noqa: BLE001
            log.error("slack_readiness_check_failed", reason=type(exc).__name__)
        await asyncio.sleep(interval)


async def refresh_then_watch_slack(
    snapshot: TenantSlackSettings,
    service: PostingService,
    interval: float = READINESS_RETRY_SECONDS,
) -> None:
    """Refresh the snapshot, then keep the service ready, while the server already listens."""
    await snapshot.refresh_then_start()
    await watch_slack(service, interval)


def main() -> None:
    from services.server_tls import expires_at, wrap

    logging.basicConfig(level=configured_log_level())
    settings = get_settings()
    try:
        tenants = shared_app_tenants(settings)
    except ConfigError as exc:
        raise SystemExit(f"the posting service has no tenant to post for. {exc}") from exc
    certificate, key = settings.server_tls_certificate_file, settings.server_tls_key_file
    if not (settings.server_tls and certificate and key):
        raise SystemExit(
            "the posting service serves only a certificate its callers verify. Set SERVER_TLS, "
            "SERVER_TLS_CERTIFICATE_FILE, and SERVER_TLS_KEY_FILE"
        )
    try:
        platform_escalation_preflight()
    except ConfigError as exc:
        raise SystemExit(f"the posting service refuses to start, because {exc}") from exc
    snapshot = tenant_slack_settings(settings, tenants)
    service = PostingService(
        identity=WorkerIdentity(KubernetesTokenReview.in_cluster(), tenants),
        snapshot=snapshot,
        slack_client=SlackClients(settings.slack_shared_bot_token),
        owner_key=owner_key(settings.slack_shared_bot_token),
        admin=process_admin_destination,
    )
    server = ThreadingHTTPServer((settings.bind_host, PORT), build_handler(service))
    served = expires_at(Path(certificate))
    wrap(server, enabled=True, certificate_file=certificate, key_file=key)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="slack-poster-refresh", daemon=True).start()
    asyncio.run_coroutine_threadsafe(refresh_then_watch_slack(snapshot, service), loop)
    asyncio.run_coroutine_threadsafe(watch_leaf(served, Path(certificate)), loop)
    log.info("slack_poster_listening", url=f"https://{settings.bind_host}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
