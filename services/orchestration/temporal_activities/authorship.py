"""Temporal activities for authored-identity signing and merge-gate verification.

Signing and verification shell out to ssh-keygen and git, so they run here in
activities rather than in the workflow sandbox. Each activity is a no-op or a
returned ``None`` on a backend without the authorship capability, so a
non-participating deployment runs the same path as before.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from services.orchestration.authorship import (
    AuthorshipRecord,
    authorship_signing_active,
    sign_artifact_authorship,
    store_repo_root,
    verify_authorship_at_merge,
)


@dataclass
class SignAuthorshipRequest:
    execution_id: str
    kind: str
    artifact: str
    commit: str = ""


@activity.defn
async def sign_authorship(req: SignAuthorshipRequest) -> dict[str, Any] | None:
    artifact_hash = hashlib.sha256(req.artifact.encode("utf-8")).hexdigest()
    record = sign_artifact_authorship(
        execution_id=req.execution_id,
        kind=req.kind,
        artifact_hash=artifact_hash,
        commit=req.commit or None,
    )
    return record.to_dict() if record is not None else None


@dataclass
class VerifyAuthorshipRequest:
    records: list[dict[str, Any]] = field(default_factory=list)


@activity.defn
async def verify_authorship_merge(req: VerifyAuthorshipRequest) -> None:
    if not authorship_signing_active():
        return
    records = [AuthorshipRecord.from_dict(entry) for entry in req.records]
    verify_authorship_at_merge(records, repo_root=store_repo_root())
