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
    """Return the Temporal schedule id for a source, honoring an explicit override."""
    return settings.intake_schedule_id or f"{source}-intake"


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
    return 1 if result.blocked_by or result.undetermined else 0


async def install_schedule(source: str, client: Any = None) -> int:
    """Register the intake schedule for a source and remove another source's schedule."""
    from services.orchestration.temporal_workflows.intake import IntakeInput, IntakeWorkflow
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleSpec,
    )
    from temporalio.service import RPCError, RPCStatusCode

    settings = get_settings()
    if client is None:
        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    schedule_id = intake_schedule_id(settings, source)
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IntakeWorkflow.run,
            IntakeInput(),
            id=f"{schedule_id}-run",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(seconds=settings.intake_schedule_interval_seconds)
                )
            ]
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

    for other in TICKET_BACKENDS:
        other_id = f"{other}-intake"
        if other == source or other_id == schedule_id:
            continue
        try:
            await client.get_schedule_handle(other_id).delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            continue
        print(f"[intake:{source}] removed schedule '{other_id}' for the inactive {other} source")
    return 0


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
    source = resolve_intake_source(args.source, get_settings())
    if args.retry:
        return asyncio.run(_retry(source, args.retry, args.repo))
    if args.schedule:
        return asyncio.run(install_schedule(source))
    return asyncio.run(_run_once(source))


if __name__ == "__main__":
    raise SystemExit(main())
