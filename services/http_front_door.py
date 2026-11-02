"""Request limits and shared-secret checks for the BaseHTTPRequestHandler services."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import BinaryIO

MAX_REQUEST_BODY_BYTES = 1_048_576
REQUEST_TIMEOUT_SECONDS = 30.0

_CONTENT_LENGTH_HEADER = "Content-Length"
_AUTHORIZATION_HEADER = "Authorization"
_BEARER_PREFIX = "Bearer "


class RequestRejected(Exception):
    """Carries the HTTP status and body a front door returns for a refused request."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def declared_body_length(
    headers: Mapping[str, str], *, max_bytes: int = MAX_REQUEST_BODY_BYTES
) -> int:
    raw = (headers.get(_CONTENT_LENGTH_HEADER) or "").strip()
    if not raw:
        return 0
    if not (raw.isascii() and raw.isdigit()):
        raise RequestRejected(400, "invalid content-length")
    length = int(raw)
    if length > max_bytes:
        raise RequestRejected(413, "request body too large")
    return length


def read_bounded_body(
    rfile: BinaryIO, headers: Mapping[str, str], *, max_bytes: int = MAX_REQUEST_BODY_BYTES
) -> bytes:
    length = declared_body_length(headers, max_bytes=max_bytes)
    return rfile.read(length) if length else b""


def bearer_token_accepted(headers: Mapping[str, str], expected: str) -> bool:
    if not expected:
        return False
    presented = headers.get(_AUTHORIZATION_HEADER) or ""
    if not presented.startswith(_BEARER_PREFIX):
        return False
    return hmac.compare_digest(presented[len(_BEARER_PREFIX) :], expected)


def bearer_header(token: str) -> dict[str, str]:
    return {_AUTHORIZATION_HEADER: f"{_BEARER_PREFIX}{token}"}
