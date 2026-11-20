"""Temporal activity that reaps TTL-expired orphan artifacts and orphaned sandbox pods."""

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
    from services.integration.github.factory import build_git_host

    return build_git_host()


def _build_sandbox_sweep(settings, executions):
    from openhands.runtime.provisioner import KubernetesSandboxPods

    from services.coordination.reaper import SandboxSweep
    from services.orchestration.temporal_activities.persistence import record_audit_event

    return SandboxSweep(
        pods=KubernetesSandboxPods(in_cluster=settings.openhands_in_cluster),
        namespace=settings.openhands_sandbox_namespace,
        executions=executions,
        audit=record_audit_event,
    )


@activity.defn
async def reap_orphans(request: ReapRequest) -> ReapReport:
    from scripts.config import get_settings

    from services.coordination.reaper import reap
    from services.integration.source_control import source_control_repos

    settings = get_settings()
    registry = _build_registry()
    receipt = await reap(
        registry=registry,
        backend=_build_backend(),
        allowed_repos=frozenset(source_control_repos(settings)),
        now=datetime.now(UTC),
        tenant=settings.tenant,
        dry_run=request.dry_run,
        sandbox=_build_sandbox_sweep(settings, registry),
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
