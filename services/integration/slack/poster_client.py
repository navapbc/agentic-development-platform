"""Client of the Slack posting service, through which a tenant on the shared Slack app posts alerts.

The posting service holds the shared app's bot token and posts to the channel the tenant's latest
escalation.yaml names, so a request carries only the message text and blocks. The worker proves
its identity with the projected service account token at SLACK_POSTING_TOKEN_FILE, which the
kubelet rotates, so the file is read for every post. The client verifies the service's certificate
against the CA file SLACK_POSTING_CA_FILE names and no other trust store, and keeps one connection
pool per CA file, which it replaces when the file changes, so a rotated CA mounted in place takes
effect on the next post.
"""

from __future__ import annotations

import os
import ssl
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from scripts.config import Settings

from services.http_front_door import bearer_header
from services.slack_poster import DEADLINE_HEADER, MESSAGES_PATH, MIN_DEADLINE_SECONDS

DEADLINE_MARGIN_SECONDS = 1

_clients: dict[str, tuple[int, httpx.Client]] = {}
_clients_lock = threading.Lock()


class PostingFailed(Exception):
    """The posting service did not post the message."""


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
    return str(error) if error else "no error code"


def post_message(
    settings: Settings, *, text: str, blocks: list[dict[str, Any]] | None, timeout: float
) -> None:
    """Post one message, bounded by ``timeout`` seconds, or raise PostingFailed."""
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
    body: dict[str, Any] = {"text": text}
    if blocks is not None:
        body["blocks"] = blocks
    try:
        response = client.post(
            base_url + MESSAGES_PATH,
            json=body,
            headers={**bearer_header(token), DEADLINE_HEADER: deadline},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise PostingFailed(
            f"the Slack posting service is unreachable ({unreachable_reason(exc)})"
        ) from None
    if response.status_code != 200:
        raise PostingFailed(
            f"the Slack posting service refused the post with {_error_code(response)} "
            f"(HTTP {response.status_code})"
        )


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
