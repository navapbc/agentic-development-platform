"""Platform-issued HMAC attestation over a normalized review verdict."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from services.review.types import Attestation

ALGORITHM = "HMAC-SHA256"
PAYLOAD_VERSION = 1


class ReviewAttestationError(Exception):
    """A review attestation failed to verify; the caller must fail closed."""


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def key_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


@runtime_checkable
class Attestor(Protocol):
    def sign(self, payload: dict[str, Any]) -> Attestation: ...

    def verify(self, attestation: Attestation, payload: dict[str, Any]) -> bool: ...


class HmacAttestor:
    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")
        self._key_id = key_fingerprint(secret)

    def sign(self, payload: dict[str, Any]) -> Attestation:
        digest = content_hash(payload)
        signature = hmac.new(self._secret, digest.encode("utf-8"), hashlib.sha256).hexdigest()
        return Attestation(
            algorithm=ALGORITHM,
            payload_version=PAYLOAD_VERSION,
            content_hash=digest,
            signature=signature,
            key_id=self._key_id,
            issued_at=datetime.now(UTC).isoformat(),
        )

    def verify(self, attestation: Attestation, payload: dict[str, Any]) -> bool:
        if attestation.algorithm != ALGORITHM:
            return False
        if attestation.content_hash != content_hash(payload):
            return False
        expected = hmac.new(
            self._secret, attestation.content_hash.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, attestation.signature)
