"""Authored-identity signing over the rebar authorship substrate.

Provisions an authoring agent's rebar identity and keyring, and resolves the
identity trust root. Signing and merge-gate verification build on this. The
capability is active only on the rebar backend with an identity configured. A
Jira deployment has no rebar authorship substrate and does not participate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.config import Settings, get_settings

from services.orchestration.signing_keys import SigningKeyError, authorship_signing_key_path

log = logging.getLogger(__name__)

AUTHORSHIP_PLAN_KIND = "plan"
AUTHORSHIP_CODE_KIND = "code"
AUTHORSHIP_KINDS = (AUTHORSHIP_PLAN_KIND, AUTHORSHIP_CODE_KIND)


class AuthorshipConfigError(RuntimeError):
    """Authored-identity signing is active but the identity or key is not usable."""


class AuthorshipVerificationError(RuntimeError):
    """A required authorship signature is absent or does not verify; fail closed."""


class AuditSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class AuthorshipRecord:
    """A signed authorship claim: the event it binds and the DSSE envelope over it."""

    kind: str
    identity_id: str
    event: dict[str, Any]
    envelope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identity_id": self.identity_id,
            "event": self.event,
            "envelope": self.envelope,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthorshipRecord:
        return cls(
            kind=data["kind"],
            identity_id=data["identity_id"],
            event=data["event"],
            envelope=data["envelope"],
        )


def authorship_signing_active(settings: Settings | None = None) -> bool:
    """Report whether authored-identity signing applies to this deployment.

    Active when signing is enabled, the ticket backend is rebar, and an identity
    is configured. A Jira backend, a missing identity, or a disabled flag all
    yield inactive, so a non-participating deployment behaves as before.
    """
    resolved = settings or get_settings()
    if not resolved.authorship_signing_enabled:
        return False
    from services.integration.tickets.backend import REBAR
    from services.integration.tickets.factory import resolve_ticket_backend_name

    if resolve_ticket_backend_name(resolved) != REBAR:
        return False
    return bool(resolved.authorship_identity_id)


def store_repo_root(settings: Settings | None = None) -> str:
    from services.integration.tickets.rebar_config import load_rebar_backend_config

    return load_rebar_backend_config(settings or get_settings()).repo_root


def public_key_line(key_path: str) -> str:
    pub = Path(f"{key_path}.pub")
    if not pub.is_file():
        raise AuthorshipConfigError(f"authorship signing public key not found at {pub}")
    return pub.read_text(encoding="utf-8").strip()


def signing_key_path(settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    try:
        key_path = authorship_signing_key_path(resolved)
    except SigningKeyError as exc:
        raise AuthorshipConfigError(str(exc)) from exc
    if not key_path or not Path(key_path).is_file():
        raise AuthorshipConfigError(
            f"authorship signing key not found at {key_path!r}. Provision the key or set "
            "AUTHORSHIP_SIGNING_ENABLED=false."
        )
    return key_path


def seed_genesis_key(identity_id: str, public_key: str, *, repo_root: str) -> bool:
    """Seed the identity's genesis key trust-on-first-use when it is keyless.

    Returns True when a key was seeded, False when the identity already holds one.
    A second seed on a keyed identity is left to rebar, which refuses an unsigned
    non-genesis add.
    """
    import rebar
    from rebar.attest.authorship import resolve_trust_root

    if resolve_trust_root(identity_id, repo_root=repo_root) is not None:
        return False
    rebar.add_identity_key(identity_id, public_key, repo_root=repo_root)
    return True


def create_authoring_identity(name: str, email: str, public_key: str, *, repo_root: str) -> str:
    """Mint a rebar identity and seed its genesis key, returning the identity id."""
    import rebar
    from rebar.attest.authorship import resolve_trust_root

    identity_id = str(rebar.create_identity(name, email, repo_root=repo_root))
    rebar.add_identity_key(identity_id, public_key, repo_root=repo_root)
    if resolve_trust_root(identity_id, repo_root=repo_root) is None:
        raise AuthorshipConfigError(
            f"identity {identity_id!r} has no resolvable trust root after seeding"
        )
    return identity_id


def resolve_authoring_trust_root(identity_id: str, *, repo_root: str) -> str:
    """Resolve the identity's trust root, raising when it cannot be resolved."""
    from rebar.attest.authorship import resolve_trust_root

    trust_root = resolve_trust_root(identity_id, repo_root=repo_root)
    if trust_root is None:
        raise AuthorshipConfigError(
            f"no resolvable authorship trust root for identity {identity_id!r}"
        )
    return trust_root


def provision_authoring_identity(
    settings: Settings | None = None, *, repo_root: str | None = None
) -> str:
    """Resolve the configured identity and ensure its genesis key is seeded.

    Returns the identity id. Raises when signing is active but the identity is
    absent, is not an identity ticket, or the signing key is missing.
    """
    resolved = settings or get_settings()
    if not authorship_signing_active(resolved):
        raise AuthorshipConfigError("authored-identity signing is not active for this deployment")
    identity_id = resolved.authorship_identity_id
    root = repo_root or store_repo_root(resolved)
    key_path = signing_key_path(resolved)
    public_key = public_key_line(key_path)

    import rebar

    try:
        ticket = rebar.show_ticket(identity_id, repo_root=root)
    except Exception as exc:  # noqa: BLE001 - surface a clear config error
        raise AuthorshipConfigError(
            f"authorship identity {identity_id!r} could not be read from the rebar store"
        ) from exc
    if ticket is None:
        raise AuthorshipConfigError(
            f"authorship identity {identity_id!r} not found in the rebar store"
        )
    if ticket.get("ticket_type") != "identity":
        raise AuthorshipConfigError(f"ticket {identity_id!r} is not an identity ticket")

    seed_genesis_key(identity_id, public_key, repo_root=root)
    resolve_authoring_trust_root(identity_id, repo_root=root)
    return identity_id


def authorship_signing_preflight(settings: Settings | None = None) -> None:
    """Fail the worker fast when authored-identity signing is active but unusable.

    A no-op on a backend without the capability. On the rebar backend with an
    identity configured it checks ssh-keygen, the signing key, and that the identity
    resolves a trust root in the store, so a misconfiguration surfaces at startup
    rather than on the first signed run.
    """
    resolved = settings or get_settings()
    if not authorship_signing_active(resolved):
        return
    from rebar.attest.sshsig import ensure_available

    ensure_available()
    provision_authoring_identity(resolved)


def build_authorship_event(
    *, execution_id: str, kind: str, artifact_hash: str, commit: str | None = None
) -> dict[str, Any]:
    """Build the authorship event a signature binds for a produced artifact."""
    return {
        "uuid": f"{execution_id}:{kind}",
        "kind": kind,
        "execution_id": execution_id,
        "artifact_hash": artifact_hash,
        "commit": commit or "",
    }


def _encode_envelope(envelope: Any) -> str:
    from rebar.attest import dsse

    return dsse.encode(
        envelope.payload_type,
        envelope.payload,
        [{"keyid": sig.keyid, "sig": sig.sig} for sig in envelope.signatures],
    )


def sign_authorship_event(event: dict[str, Any], *, key_path: str, identity_id: str) -> str:
    """Sign an authorship event under ``identity_id`` and return the encoded envelope."""
    from rebar.attest.authorship import sign_event_authorship

    envelope = sign_event_authorship(event, key_path, identity_id)
    return _encode_envelope(envelope)


def sign_artifact_authorship(
    *,
    execution_id: str,
    kind: str,
    artifact_hash: str,
    commit: str | None = None,
    settings: Settings | None = None,
) -> AuthorshipRecord | None:
    """Sign a produced artifact's authorship, or return None on a non-capable backend.

    Returns an :class:`AuthorshipRecord` when signing is active. Returns None when
    the backend does not participate, so the caller proceeds unsigned. Raises when
    signing is active but the key is missing, so a capable backend never silently
    skips the signature.
    """
    resolved = settings or get_settings()
    if kind not in AUTHORSHIP_KINDS:
        raise ValueError(f"unknown authorship kind {kind!r}")
    if not authorship_signing_active(resolved):
        return None
    key_path = signing_key_path(resolved)
    identity_id = resolved.authorship_identity_id
    event = build_authorship_event(
        execution_id=execution_id, kind=kind, artifact_hash=artifact_hash, commit=commit
    )
    envelope = sign_authorship_event(event, key_path=key_path, identity_id=identity_id)
    return AuthorshipRecord(kind=kind, identity_id=identity_id, event=event, envelope=envelope)


def verify_authorship_record(record: AuthorshipRecord, *, repo_root: str) -> bool:
    """Verify a single authorship record against the identity's current trust root."""
    from rebar.attest import dsse
    from rebar.attest.authorship import verify_event_authorship

    try:
        envelope = dsse.decode(record.envelope)
    except Exception:  # noqa: BLE001 - a malformed envelope fails closed, never raises
        return False
    verdict = verify_event_authorship(
        record.event, envelope, record.identity_id, repo_root=repo_root
    )
    return bool(verdict.verified)


def verify_authorship_at_merge(
    records: list[AuthorshipRecord],
    *,
    repo_root: str,
    settings: Settings | None = None,
) -> None:
    """Fail closed unless the plan and code authorship both verify.

    A no-op on a backend without the authorship capability. On a capable backend a
    missing, tampered, invalid, or out-of-trust-root signature raises
    :class:`AuthorshipVerificationError`, blocking the merge.
    """
    resolved = settings or get_settings()
    if not authorship_signing_active(resolved):
        return
    present = {record.kind for record in records}
    missing = [kind for kind in AUTHORSHIP_KINDS if kind not in present]
    if missing:
        raise AuthorshipVerificationError(f"missing authorship signatures for {', '.join(missing)}")
    for record in records:
        if not verify_authorship_record(record, repo_root=repo_root):
            raise AuthorshipVerificationError(
                f"authorship signature for {record.kind} by {record.identity_id!r} did not verify"
            )


def _sign_keyop(op: str, identity_id: str, public_key: str, *, key_path: str) -> Any:
    from rebar.attest.authorship import keyop_payload, sign_authorship

    payload = keyop_payload(op, identity_id, public_key)
    return sign_authorship(payload, key_path, identity_id)


def _keyring_audit_event(op: str, identity_id: str, public_key: str, actor: str) -> dict[str, Any]:
    return {
        "event": "authorship_keyring_change",
        "op": op,
        "identity_id": identity_id,
        "public_key": public_key,
        "actor": actor,
    }


def rotate_identity_key(
    identity_id: str,
    new_public_key: str,
    *,
    current_key_path: str,
    repo_root: str,
    audit: AuditSink,
    actor: str = "authorship",
) -> None:
    """Admit ``new_public_key`` with a key-operation signed by a currently-valid key.

    The rebar keyring refuses an unsigned or invalidly signed non-genesis add, so a
    rotation without authorization fails. Writes an audit record for the change.
    """
    import rebar

    signature = _sign_keyop("KEY_ADD", identity_id, new_public_key, key_path=current_key_path)
    rebar.add_identity_key(identity_id, new_public_key, signature=signature, repo_root=repo_root)
    audit.record(_keyring_audit_event("KEY_ADD", identity_id, new_public_key, actor))


def revoke_identity_key(
    identity_id: str,
    public_key: str,
    *,
    current_key_path: str,
    repo_root: str,
    audit: AuditSink,
    actor: str = "authorship",
) -> None:
    """Revoke ``public_key`` with a key-operation signed by a currently-valid key.

    Writes an audit record for the change. After revocation the key no longer
    resolves into the identity trust root, so its signatures stop verifying.
    """
    import rebar

    signature = _sign_keyop("KEY_REVOKE", identity_id, public_key, key_path=current_key_path)
    rebar.revoke_identity_key(identity_id, public_key, signature=signature, repo_root=repo_root)
    audit.record(_keyring_audit_event("KEY_REVOKE", identity_id, public_key, actor))
