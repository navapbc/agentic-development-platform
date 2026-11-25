"""Client of the Slack posting service, through which a tenant on the shared Slack app posts alerts.

The posting service holds the shared app's bot token and posts to the channel the tenant's latest
escalation.yaml names, so a request carries only the message text and blocks. The worker proves
its identity with the projected service account token at SLACK_POSTING_TOKEN_FILE, which the
kubelet rotates, so the file is read for every post. The client verifies the service's certificate
against the CA file SLACK_POSTING_CA_FILE names and no other trust store, and keeps one connection
pool per CA file, which it replaces when the file changes, so a rotated CA mounted in place takes
effect on the next post.

A new admin post answers with an owner token, which an edit of the post or a reply in its thread
presents, so the caller stores it with the post's ts.

A worker of a shared-app tenant reads the members of a Slack user group its escalation.yaml names
through the same service, with the same token, CA, and deadline header.
"""

from __future__ import annotations

import os
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from scripts.config import Settings

from services.http_front_door import bearer_header
from services.slack_poster import (
    ADMIN_AUDIENCE,
    DEADLINE_HEADER,
    GROUPS_PATH,
    MESSAGES_PATH,
    MIN_DEADLINE_SECONDS,
    OWNER_PROOF_FIELD,
    UPDATE_PATH,
)

DEADLINE_MARGIN_SECONDS = 1

_clients: dict[str, tuple[int, httpx.Client]] = {}
_clients_lock = threading.Lock()


@dataclass(frozen=True)
class PostedMessage:
    ts: str
    owner_token: str = ""


class PostingFailed(Exception):
    """The posting service did not serve the request.

    ``error`` holds the error code the service answered with, and is empty when the request did
    not reach the service or the service answered without a code.
    """

    def __init__(self, reason: str, *, error: str = "") -> None:
        super().__init__(reason)
        self.error = error


def _client(ca_file: str) -> httpx.Client:
    with _clients_lock:
        try:
            modified = os.stat(ca_file).st_mtime_ns
            cached = _clients.get(ca_file)
            if cached is not None and cached[0] == modified:
                return cached[1]
            context = ssl.create_default_context(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise PostingFailed(
                f"SLACK_POSTING_CA_FILE {ca_file!r} holds no readable CA certificate "
                f"({type(exc).__name__})"
            ) from None
        client = httpx.Client(verify=context, follow_redirects=False)
        if cached is not None:
            cached[1].close()
        _clients[ca_file] = (modified, client)
        return client


def unreachable_reason(exc: BaseException) -> str:
    """Name the certificate verification failure behind exc, or else exc's type."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return f"certificate verification failed ({cause.verify_message})"
        cause = cause.__cause__ or cause.__context__
    return type(exc).__name__


def _token(settings: Settings) -> str:
    path = settings.slack_posting_token_file
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PostingFailed(
            f"SLACK_POSTING_TOKEN_FILE {path!r} could not be read ({type(exc).__name__})"
        ) from None


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    return str(error) if error else ""


def _send(settings: Settings, path: str, *, timeout: float, body: Any = None) -> httpx.Response:
    base_url = settings.slack_posting_url.rstrip("/")
    if urlsplit(base_url).scheme != "https":
        raise PostingFailed("SLACK_POSTING_URL must be an https URL")
    if not settings.slack_posting_ca_file:
        raise PostingFailed(
            "SLACK_POSTING_CA_FILE is not set, so the posting service's certificate cannot be "
            "verified"
        )
    deadline = _server_deadline(timeout)
    client = _client(settings.slack_posting_ca_file)
    token = _token(settings)
    headers = {**bearer_header(token), DEADLINE_HEADER: deadline}
    try:
        if body is None:
            return client.get(base_url + path, headers=headers, timeout=timeout)
        return client.post(base_url + path, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise PostingFailed(
            f"the Slack posting service is unreachable ({unreachable_reason(exc)})"
        ) from None


def _refused(response: httpx.Response, request: str) -> PostingFailed:
    error = _error_code(response)
    return PostingFailed(
        f"the Slack posting service refused the {request} with {error or 'no error code'} "
        f"(HTTP {response.status_code})",
        error=error,
    )


def _message_body(
    text: str,
    blocks: list[dict[str, Any]] | None,
    admin: bool,
    *,
    owner_token: str = "",
    **stamps: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text, **{key: value for key, value in stamps.items() if value}}
    if owner_token:
        body[OWNER_PROOF_FIELD] = owner_token
    if blocks is not None:
        body["blocks"] = blocks
    if admin:
        body["audience"] = ADMIN_AUDIENCE
    return body


def _posted(response: httpx.Response) -> PostedMessage:
    try:
        answer = response.json()
        ts, owner_token = answer.get("ts"), answer.get(OWNER_PROOF_FIELD)
    except ValueError, AttributeError:
        ts = owner_token = None
    return PostedMessage(
        ts if isinstance(ts, str) else "", owner_token if isinstance(owner_token, str) else ""
    )


def post_message(
    settings: Settings,
    *,
    text: str,
    blocks: list[dict[str, Any]] | None,
    timeout: float,
    thread_ts: str = "",
    admin: bool = False,
    owner_token: str = "",
) -> PostedMessage:
    """Post one message, bounded by ``timeout`` seconds, and return its ts and owner token, or
    raise PostingFailed.

    ``admin`` posts to the admin channel of platform-escalation.yaml in place of the tenant's. A
    reply in the thread of an admin post presents the post's ``owner_token``.
    """
    body = _message_body(text, blocks, admin, thread_ts=thread_ts, owner_token=owner_token)
    response = _send(settings, MESSAGES_PATH, timeout=timeout, body=body)
    if response.status_code != 200:
        raise _refused(response, "post")
    return _posted(response)


def update_message(
    settings: Settings,
    *,
    ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None,
    timeout: float,
    admin: bool = False,
    owner_token: str = "",
) -> None:
    """Replace the text and blocks of a message the service posted, or raise PostingFailed.

    An edit of an admin post presents the ``owner_token`` its post answered with.
    """
    body = _message_body(text, blocks, admin, ts=ts, owner_token=owner_token)
    response = _send(settings, UPDATE_PATH, timeout=timeout, body=body)
    if response.status_code != 200:
        raise _refused(response, "update")


def read_group_members(settings: Settings, group_id: str, *, timeout: float) -> list[str]:
    """Return the user IDs of a Slack user group the tenant names, or raise PostingFailed."""
    response = _send(settings, GROUPS_PATH + group_id, timeout=timeout)
    if response.status_code != 200:
        raise _refused(response, "group read")
    try:
        members = response.json().get("members")
    except ValueError, AttributeError:
        members = None
    if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
        raise PostingFailed("the Slack posting service answered the group read without members")
    return members


def _server_deadline(timeout: float) -> str:
    """Return the deadline header, or refuse when the service would receive no time for Slack.

    The service spends part of the deadline on the TokenReview and refuses a post left with less
    than MIN_DEADLINE_SECONDS, so a header at that minimum would always be refused.
    """
    seconds = int(timeout) - DEADLINE_MARGIN_SECONDS
    if seconds <= MIN_DEADLINE_SECONDS:
        raise PostingFailed(
            f"only {timeout:.1f} seconds of the alert deadline are left, too little for the "
            "Slack posting service to post"
        )
    return str(seconds)
