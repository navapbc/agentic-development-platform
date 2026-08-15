"""Temporal activity for reaping TTL-expired platform orphan artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from temporalio import activity


@dataclass
class ReapRequest:
    dry_run: bool = True


@dataclass
class ReapActionRecord:
    orphan_id: str
    artifact_type: str
    external_id: str
    repository: str
    action: str
    applied: bool
    reason: str


@dataclass
class ReapReport:
    dry_run: bool
    scanned: int
    applied: int
    actions: list[ReapActionRecord] = field(default_factory=list)


def _build_registry():
    from services.coordination.orphans import PostgresOrphanRegistry

    return PostgresOrphanRegistry()


def _build_backend():
    from adapters.real.git_host import RealGitHost

    return RealGitHost.from_settings()


@activity.defn
async def reap_orphans(request: ReapRequest) -> ReapReport:
    from scripts.config import get_settings

    from services.coordination.reaper import reap
    from services.integration.github.credentials import allowed_repos

    settings = get_settings()
    receipt = await reap(
        registry=_build_registry(),
        backend=_build_backend(),
        allowed_repos=frozenset(allowed_repos(settings)),
        now=datetime.now(UTC),
        dry_run=request.dry_run,
    )
    return ReapReport(
        dry_run=receipt.dry_run,
        scanned=receipt.scanned,
        applied=len(receipt.applied),
        actions=[
            ReapActionRecord(
                orphan_id=action.orphan_id,
                artifact_type=action.artifact_type,
                external_id=action.external_id,
                repository=action.repository,
                action=action.action,
                applied=action.applied,
                reason=action.reason,
            )
            for action in receipt.actions
        ],
    )
