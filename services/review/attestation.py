"""Platform-issued attestation over a normalized review verdict.

Two attestors implement the :class:`Attestor` protocol. :class:`HmacAttestor` is
the symmetric local-development fallback. :class:`SshsigAttestor` signs the
canonical verdict payload with the rebar SSHSIG scheme inside a DSSE envelope and
verifies it against a public trust root, so a verifier holds only public keys and
cannot forge a verdict.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from services.review.types import Attestation

ALGORITHM = "HMAC-SHA256"
PAYLOAD_VERSION = 1

SSHSIG_ALGORITHM = "sshsig"
REVIEW_VERDICT_KIND = "nava.review.verdict.v1"
REVIEW_VERDICT_NAMESPACE = "review-verdict@nava"
REVIEW_PAYLOAD_TYPE = "application/vnd.nava.review-verdict+json"


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


def _register_review_scheme() -> None:
    """Register the SSHSIG scheme and pin the review-verdict policy (idempotent).

    The policy table selects the scheme by kind, never from the envelope, so a
    record cannot influence which scheme verifies it (the alg-confusion class).
    """
    from rebar.attest import registry
    from rebar.attest.sshsig import register_sshsig_scheme

    if registry.get_scheme(SSHSIG_ALGORITHM) is None:
        register_sshsig_scheme()
    registry.POLICY[REVIEW_VERDICT_KIND] = registry.Policy(
        scheme=SSHSIG_ALGORITHM,
        namespace=REVIEW_VERDICT_NAMESPACE,
    )


class SshsigAttestor:
    """Sign and verify review verdicts with SSHSIG-over-DSSE against a trust root.

    ``trust_root`` is an OpenSSH ``allowed_signers`` blob binding the trusted public
    keys to ``principal``. Signing needs the private key at ``key_path``; verifying
    needs only the trust root, so a verifier cannot forge a verdict.
    """

    def __init__(self, *, key_path: str, principal: str, trust_root: str) -> None:
        from rebar.attest.sshsig import ensure_available

        ensure_available()
        _register_review_scheme()
        self._key_path = key_path
        self._principal = principal
        self._trust_root = trust_root

    def sign(self, payload: dict[str, Any]) -> Attestation:
        from rebar.attest import dsse
        from rebar.attest.sshsig import sign as sshsig_sign

        body = canonical_bytes(payload)
        pae = dsse.pae(REVIEW_PAYLOAD_TYPE, body)
        signature = sshsig_sign(pae, self._key_path, REVIEW_VERDICT_NAMESPACE)
        envelope = dsse.encode(
            REVIEW_PAYLOAD_TYPE,
            body,
            [{"keyid": self._principal, "sig": signature}],
        )
        return Attestation(
            algorithm=SSHSIG_ALGORITHM,
            payload_version=PAYLOAD_VERSION,
            content_hash=content_hash(payload),
            signature=base64.b64encode(signature).decode("ascii"),
            key_id=self._principal,
            issued_at=datetime.now(UTC).isoformat(),
            envelope=envelope,
        )

    def verify(self, attestation: Attestation, payload: dict[str, Any]) -> bool:
        from rebar.attest import dsse, registry

        if attestation.algorithm != SSHSIG_ALGORITHM or not attestation.envelope:
            return False
        try:
            envelope = dsse.decode(attestation.envelope)
        except Exception:  # noqa: BLE001 - a malformed envelope fails closed, never raises
            return False
        if envelope.payload != canonical_bytes(payload):
            return False
        verdict = registry.verify(REVIEW_VERDICT_KIND, envelope, self._trust_root)
        return bool(verdict.verified)
