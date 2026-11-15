"""Ticket intake runner for the Jira and rebar sources: a single pass, a schedule, or a retry."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from typing import Any

from services.integration.tickets import TICKET_BACKENDS, resolve_ticket_backend_name

from scripts.config import Settings, get_settings


class IntakeSourceMismatch(SystemExit):
    """The requested intake source is not the configured ticket backend."""


def intake_schedule_id(settings: Settings, source: str) -> str:
    """Return the Temporal schedule id for a source and tenant, honoring an explicit override."""
    from services.orchestration.task_queues import tenant_schedule_id

    return settings.intake_schedule_id or tenant_schedule_id(f"{source}-intake", settings)


def resolve_intake_source(requested: str | None, settings: Settings) -> str:
    """Return the intake source, which must match TICKET_BACKEND.

    The ticket write-back and the execution records follow TICKET_BACKEND, so intake from a
    different source would start work the rest of the platform cannot track.
    """
    configured = resolve_ticket_backend_name(settings)
    source = (requested or configured).strip().lower()
    if source != configured:
        raise IntakeSourceMismatch(
            f"[intake:{source}] TICKET_BACKEND is {configured}, so intake runs from "
            f"{configured}. Pass --source {configured}, or set TICKET_BACKEND={source}."
        )
    return source


async def _run_once(source: str) -> int:
    from services.orchestration.temporal_activities.intake import (
        IntakeReconcileRequest,
        reconcile_intake_pass,
    )

    report = await reconcile_intake_pass(IntakeReconcileRequest())
    print(
        f"[intake:{source}] started={report.started} duplicates={report.duplicates} "
        f"held={report.held}"
    )
    return 0


async def _retry(source: str, ticket_key: str, repo: str | None) -> int:
    from services.orchestration.intake_retry import run_intake_retry

    result = await run_intake_retry(ticket_key, repo=repo)
    print(f"[intake:{source}] {result.message}")
    return 0 if result.retriggered else 1


async def install_schedule(source: str, client: Any = None) -> int:
    """Register the intake schedule for a source and remove another source's schedule.

    The schedule installs paused, with the reason as its note, while the source cannot
    authenticate with this process's credentials.
    """
    from services import temporal_client
    from services.integration.intake_readiness import intake_blocker
    from services.orchestration.task_queues import run_task_queue, tenant_schedule_id
    from services.orchestration.temporal_workflows.intake import IntakeInput, IntakeWorkflow
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleState,
    )
    from temporalio.service import RPCError, RPCStatusCode

    settings = get_settings()
    blocker = intake_blocker(source, settings)
    if client is None:
        client = await temporal_client.connect(settings.temporal_host, settings)
    schedule_id = intake_schedule_id(settings, source)
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IntakeWorkflow.run,
            IntakeInput(),
            id=f"{schedule_id}-run",
            task_queue=run_task_queue(settings),
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(seconds=settings.intake_schedule_interval_seconds)
                )
            ]
        ),
        # A run missed while Temporal was down, as the off-hours schedule leaves it, starts at
        # most once on wake instead of once for every missed interval.
        policy=SchedulePolicy(
            catchup_window=timedelta(seconds=settings.intake_schedule_interval_seconds)
        ),
        state=ScheduleState(
            paused=bool(blocker), note=f"paused because {blocker}" if blocker else ""
        ),
    )
    handle = client.get_schedule_handle(schedule_id)
    try:
        await client.create_schedule(schedule_id, schedule)
        print(f"[intake:{source}] created schedule '{schedule_id}'")
    except Exception:
        await handle.delete()
        await client.create_schedule(schedule_id, schedule)
        print(f"[intake:{source}] replaced schedule '{schedule_id}'")
    if blocker:
        print(
            f"[intake:{source}] schedule '{schedule_id}' is paused because {blocker}. "
            "It resumes when a worker starts and the source can authenticate.",
            flush=True,
        )

    retired = [tenant_schedule_id(f"{other}-intake", settings) for other in TICKET_BACKENDS]
    retired += [f"{other}-intake" for other in TICKET_BACKENDS if settings.tenant]
    for other_id in dict.fromkeys(retired):
        if other_id == schedule_id:
            continue
        try:
            await client.get_schedule_handle(other_id).delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            continue
        print(f"[intake:{source}] removed schedule '{other_id}'")
    return 0


def _export_logs() -> None:
    """Export this command's log records, the retry audit event among them, like a worker's."""
    import logging

    from observability.logs import configure_logging

    from scripts.config import configured_log_level

    logging.basicConfig(level=configured_log_level())
    configure_logging("operator.intake")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile ticket intake from Jira or rebar into draft PR executions."
    )
    parser.add_argument(
        "--source",
        choices=TICKET_BACKENDS,
        default=None,
        help="Ticket source to take intake from. Defaults to TICKET_BACKEND and must match it.",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Install the Temporal intake schedule for the source.",
    )
    parser.add_argument(
        "--retry",
        metavar="TICKET",
        help="Clear the intake dedup for a failed ticket so the next pass relaunches it.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Scope a --retry to one repository when the ticket fanned out to several.",
    )
    args = parser.parse_args(argv)
    _export_logs()
    source = resolve_intake_source(args.source, get_settings())
    if args.retry:
        return asyncio.run(_retry(source, args.retry, args.repo))
    if args.schedule:
        return asyncio.run(install_schedule(source))
    return asyncio.run(_run_once(source))


if __name__ == "__main__":
    from observability.logs import force_flush_logs
    from services.temporal_client import exit_without_finalization

    code = main()
    # exit_without_finalization skips the interpreter's shutdown, which would flush the batch.
    force_flush_logs()
    exit_without_finalization(code)
