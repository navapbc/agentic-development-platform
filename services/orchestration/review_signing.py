"""Review-worker startup checks for verdict signing and trust-root change auditing.

The preflight fails the worker fast when signing is enabled without a usable key or
trust root, so a misconfiguration surfaces at startup rather than on the first review.
The audit records a trust-root change to the platform audit store when the loaded
bundle differs from the last one recorded, so every change leaves an audit trail.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from scripts.config import Settings, get_settings

from services.review.trust_root import (
    TrustRoot,
    TrustRootError,
    audit_trust_root_change,
    load_trust_root,
    parse_trust_root,
)

log = logging.getLogger(__name__)

_AUDIT_EVENT_TYPE = "review_trust_root_change"
_AUDIT_ACTOR = "review-worker"
_AUDIT_TASK_ID = "system"
_AUDIT_EXECUTION_ID = "review-trust-root"


class ReviewSigningConfigError(RuntimeError):
    """Signing is enabled but the signing key or trust root is not usable."""


def review_signing_preflight(settings: Settings | None = None) -> None:
    """Fail fast when signing is enabled without a usable key and trust root."""
    resolved = settings or get_settings()
    if not resolved.review_signing_enabled:
        return
    missing = [
        name
        for name, value in (
            ("REVIEW_SIGNING_KEY_PATH", resolved.review_signing_key_path),
            ("REVIEW_TRUST_ROOT_PATH", resolved.review_trust_root_path),
        )
        if not value
    ]
    if missing:
        raise ReviewSigningConfigError(
            "review signing is enabled but "
            + ", ".join(missing)
            + " is not set. Provision the signing key and trust root, or set "
            "REVIEW_SIGNING_ENABLED=false."
        )
    if not Path(resolved.review_signing_key_path).is_file():
        raise ReviewSigningConfigError(
            f"review signing key not found at {resolved.review_signing_key_path}"
        )
    load_trust_root(resolved.review_trust_root_path)
    from rebar.attest.sshsig import ensure_available

    ensure_available()


class _CollectingSink:
    def __init__(self) -> None:
        self.event: dict[str, Any] | None = None

    def record(self, event: dict[str, Any]) -> None:
        self.event = event


def plan_trust_root_audit(old: TrustRoot | None, new: TrustRoot) -> dict[str, Any] | None:
    """Return the audit event for a trust-root change, or None when unchanged."""
    if old is not None and old.content_fingerprint() == new.content_fingerprint():
        return None
    sink = _CollectingSink()
    audit_trust_root_change(sink, actor=_AUDIT_ACTOR, old=old, new=new)
    return sink.event


async def audit_review_trust_root(settings: Settings | None = None) -> str | None:
    """Record a trust-root change to the audit store when the bundle differs.

    Returns the change summary when a record is written, or None when signing is off
    or the bundle is unchanged since the last recorded one.
    """
    resolved = settings or get_settings()
    if not (resolved.review_signing_enabled and resolved.review_trust_root_path):
        return None
    new = load_trust_root(resolved.review_trust_root_path)
    old = await _latest_audited_trust_root()
    event = plan_trust_root_audit(old, new)
    if event is None:
        return None
    await _write_audit(new, event)
    return event["change"]


async def _latest_audited_trust_root() -> TrustRoot | None:
    from database.models.schema import audit_events
    from sqlalchemy import select

    from services.orchestration.temporal_activities.persistence import _get_engine

    stmt = (
        select(audit_events.c.artifact_hashes)
        .where(audit_events.c.event_type == _AUDIT_EVENT_TYPE)
        .order_by(audit_events.c.created_at.desc())
        .limit(1)
    )
    engine = _get_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(stmt)).first()
    if not row or not row[0]:
        return None
    snapshot = row[0].get("snapshot")
    if not snapshot:
        return None
    try:
        return parse_trust_root(snapshot)
    except TrustRootError:
        return None


async def _write_audit(new: TrustRoot, event: dict[str, Any]) -> None:
    from services.orchestration.temporal_activities.persistence import (
        AuditEvent,
        record_audit_event,
    )

    await record_audit_event(
        AuditEvent(
            event_id=f"trust-root-{new.content_fingerprint()[:40]}",
            task_id=_AUDIT_TASK_ID,
            execution_id=_AUDIT_EXECUTION_ID,
            event_type=_AUDIT_EVENT_TYPE,
            reason=event["change"],
            artifact_hashes=event,
        )
    )
