"""Jira intake reconciler runner: a single local pass or Temporal Schedule installation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from scripts.config import get_settings


async def _run_once() -> int:
    from services.orchestration.temporal_activities.intake import (
        IntakeReconcileRequest,
        reconcile_intake_pass,
    )

    report = await reconcile_intake_pass(IntakeReconcileRequest())
    print(
        f"[intake] started={report.started} duplicates={report.duplicates} held={report.held}"
    )
    return 0


async def _retry(issue_key: str, repo: str | None) -> int:
    from services.orchestration.intake_retry import run_intake_retry

    result = await run_intake_retry(issue_key, repo=repo)
    print(f"[intake] {result.message}")
    return 0 if not result.blocked_by else 1


async def _install_schedule() -> int:
    from services.orchestration.temporal_workflows.intake import IntakeInput, IntakeWorkflow
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleSpec,
    )

    settings = get_settings()
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IntakeWorkflow.run,
            IntakeInput(),
            id=f"{settings.intake_schedule_id}-run",
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
    handle = client.get_schedule_handle(settings.intake_schedule_id)
    try:
        await client.create_schedule(settings.intake_schedule_id, schedule)
        print(f"[intake] created schedule '{settings.intake_schedule_id}'")
    except Exception:
        await handle.delete()
        await client.create_schedule(settings.intake_schedule_id, schedule)
        print(f"[intake] replaced schedule '{settings.intake_schedule_id}'")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Jira intake into draft PR executions.")
    parser.add_argument(
        "--schedule", action="store_true", help="Install the Temporal intake schedule."
    )
    parser.add_argument(
        "--retry",
        metavar="ISSUE",
        help="Clear the intake dedup for a failed issue so the next poll relaunches it.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Scope a --retry to one repository when the issue fanned out to several.",
    )
    args = parser.parse_args()
    if args.retry:
        return asyncio.run(_retry(args.retry, args.repo))
    if args.schedule:
        return asyncio.run(_install_schedule())
    return asyncio.run(_run_once())


if __name__ == "__main__":
    raise SystemExit(main())
