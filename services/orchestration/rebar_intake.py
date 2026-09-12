"""Rebar change-detection poll driven into runs through the shared intake path."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scripts.rebar_poll import (
    AccessPolicy,
    AuditSink,
    IdempotencyGuard,
    PollEvent,
    PollFlags,
    TicketStoreReader,
    WatermarkStore,
    run_poll,
)

from services.orchestration.intake_reconciler import (
    IntakeContext,
    IntakeReport,
    candidate_from_rebar,
    route_candidate,
)


@dataclass(frozen=True)
class RebarIntakeReport:
    started: int
    duplicates: int
    held: int
    deferred: int
    blocked: int


async def reconcile_rebar_intake(
    *,
    reader: TicketStoreReader,
    watermark: WatermarkStore,
    audit: AuditSink,
    access: AccessPolicy,
    allowed_fields: Sequence[str],
    ctx: IntakeContext,
    show_ticket: Callable[[str], dict],
    is_ready: Callable[[str], bool],
    project: str,
    default_repo: str,
    flags: PollFlags | None = None,
    idempotency: IdempotencyGuard | None = None,
) -> RebarIntakeReport:
    report = IntakeReport()
    blocked: list[str] = []

    async def handle(event: PollEvent) -> bool:
        if not is_ready(event.ticket_id):
            blocked.append(event.ticket_id)
            return False
        candidate = candidate_from_rebar(
            show_ticket(event.ticket_id), project=project, default_repo=default_repo
        )
        before_started = len(report.started)
        before_deferred = len(report.deferred)
        before_held = len(report.held)
        await route_candidate(
            candidate, project=project, component_map=None, ctx=ctx, report=report
        )
        if len(report.deferred) > before_deferred:
            return False
        return len(report.started) > before_started or len(report.held) > before_held

    await run_poll(
        reader=reader,
        watermark=watermark,
        audit=audit,
        access=access,
        allowed_fields=allowed_fields,
        flags=flags,
        intake=handle,
        idempotency=idempotency,
    )
    return RebarIntakeReport(
        started=sum(1 for record in report.started if not record.duplicate),
        duplicates=sum(1 for record in report.started if record.duplicate),
        held=len(report.held),
        deferred=len(report.deferred),
        blocked=len(blocked),
    )
