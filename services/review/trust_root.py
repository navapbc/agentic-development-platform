"""Versioned trust-root bundle for review-verdict signature verification.

The bundle lists the public signer keys the review gate trusts, each with a key
identifier and an optional validity window, and a revocation flag. The loader
resolves the keys active at a given time into an OpenSSH ``allowed_signers`` blob
the SSHSIG scheme verifies against. Overlapping validity windows give a rotation
rollout that admits a new key before the old key retires, and a revoked or
out-of-window key is never rendered into the trust root.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from rebar.attest.authorship import allowed_signers_from_keys


class TrustRootError(Exception):
    """A trust-root bundle is missing or malformed; the gate must fail closed."""


@dataclass(frozen=True)
class TrustRootKey:
    key_id: str
    public_key: str
    not_before: datetime | None = None
    not_after: datetime | None = None
    revoked: bool = False

    def active_at(self, now: datetime) -> bool:
        if self.revoked:
            return False
        if self.not_before is not None and now < self.not_before:
            return False
        return not (self.not_after is not None and now > self.not_after)


@dataclass(frozen=True)
class TrustRoot:
    version: int
    principal: str
    keys: tuple[TrustRootKey, ...]

    def active_keys(self, now: datetime | None = None) -> tuple[TrustRootKey, ...]:
        moment = now or datetime.now(UTC)
        return tuple(key for key in self.keys if key.active_at(moment))

    def active_key_ids(self, now: datetime | None = None) -> list[str]:
        return [key.key_id for key in self.active_keys(now)]

    def key_ids(self) -> list[str]:
        return [key.key_id for key in self.keys]

    def allowed_signers(self, now: datetime | None = None) -> str:
        """Render the ``allowed_signers`` blob for the keys active at ``now``.

        An empty result (no active key) yields an empty trust root, which fails
        every signature closed rather than admitting an untrusted one.
        """
        public_keys = [key.public_key for key in self.active_keys(now)]
        return allowed_signers_from_keys(public_keys, principal=self.principal)

    def to_bundle(self) -> dict[str, Any]:
        """Serialize to the mapping shape that :func:`parse_trust_root` accepts."""
        return {
            "version": self.version,
            "principal": self.principal,
            "keys": [
                {
                    "id": key.key_id,
                    "public_key": key.public_key,
                    "not_before": key.not_before.isoformat() if key.not_before else None,
                    "not_after": key.not_after.isoformat() if key.not_after else None,
                    "revoked": key.revoked,
                }
                for key in self.keys
            ],
        }

    def content_fingerprint(self) -> str:
        """A stable hash of the bundle content for change detection."""
        canonical = json.dumps(self.to_bundle(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_time(value: Any, *, field: str, key_id: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TrustRootError(f"key {key_id!r} field {field!r} must be an ISO-8601 string")
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TrustRootError(f"key {key_id!r} field {field!r} is not a valid timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_key(raw: Any) -> TrustRootKey:
    if not isinstance(raw, dict):
        raise TrustRootError("each entry in 'keys' must be a mapping")
    key_id = raw.get("id")
    public_key = raw.get("public_key")
    if not key_id or not isinstance(key_id, str):
        raise TrustRootError("each key requires a non-empty string 'id'")
    if not public_key or not isinstance(public_key, str):
        raise TrustRootError(f"key {key_id!r} requires a non-empty string 'public_key'")
    return TrustRootKey(
        key_id=key_id,
        public_key=public_key.strip(),
        not_before=_parse_time(raw.get("not_before"), field="not_before", key_id=key_id),
        not_after=_parse_time(raw.get("not_after"), field="not_after", key_id=key_id),
        revoked=bool(raw.get("revoked", False)),
    )


def parse_trust_root(data: Any) -> TrustRoot:
    """Parse a loaded mapping into a :class:`TrustRoot`, raising on malformed input."""
    if not isinstance(data, dict):
        raise TrustRootError("trust-root bundle must be a mapping")
    version = data.get("version")
    if not isinstance(version, int):
        raise TrustRootError("trust-root bundle requires an integer 'version'")
    principal = data.get("principal")
    if not principal or not isinstance(principal, str):
        raise TrustRootError("trust-root bundle requires a non-empty string 'principal'")
    raw_keys = data.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise TrustRootError("trust-root bundle requires a non-empty 'keys' list")
    keys = tuple(_parse_key(entry) for entry in raw_keys)
    ids = [key.key_id for key in keys]
    if len(set(ids)) != len(ids):
        raise TrustRootError("trust-root key identifiers must be unique")
    return TrustRoot(version=version, principal=principal, keys=keys)


def load_trust_root(path: str | Path) -> TrustRoot:
    """Load and validate a trust-root bundle from a YAML file."""
    resolved = Path(path)
    if not resolved.is_file():
        raise TrustRootError(f"trust-root bundle not found: {resolved}")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TrustRootError(f"trust-root bundle is not valid YAML: {resolved}") from exc
    return parse_trust_root(data)


class AuditSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


def diff_trust_roots(old: TrustRoot | None, new: TrustRoot) -> tuple[str, list[str]]:
    """Summarize the change from ``old`` to ``new`` and the affected key identifiers.

    Returns a short change description and the sorted set of key identifiers that
    were added, removed, or had their revocation state flip.
    """
    old_keys = {key.key_id: key for key in (old.keys if old else ())}
    new_keys = {key.key_id: key for key in new.keys}
    added = set(new_keys) - set(old_keys)
    removed = set(old_keys) - set(new_keys)
    revoked = {
        key_id
        for key_id in set(old_keys) & set(new_keys)
        if new_keys[key_id].revoked and not old_keys[key_id].revoked
    }
    affected = sorted(added | removed | revoked)
    if old is None:
        change = "initialized"
    else:
        parts = []
        if added:
            parts.append(f"added {len(added)}")
        if removed:
            parts.append(f"removed {len(removed)}")
        if revoked:
            parts.append(f"revoked {len(revoked)}")
        change = ", ".join(parts) if parts else "unchanged"
    return change, affected


def audit_trust_root_change(
    sink: AuditSink,
    *,
    actor: str,
    old: TrustRoot | None,
    new: TrustRoot,
) -> None:
    """Write an audit record naming the actor, the change, and the affected keys."""
    change, affected = diff_trust_roots(old, new)
    sink.record(
        {
            "event": "review_trust_root_change",
            "actor": actor,
            "change": change,
            "version": new.version,
            "principal": new.principal,
            "key_ids": affected,
            "fingerprint": new.content_fingerprint(),
            "snapshot": new.to_bundle(),
        }
    )
