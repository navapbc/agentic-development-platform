"""Orphan reaper runner: local dry-run inspection, apply, or Temporal Schedule installation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from services.integration.github.credentials import allowed_repos

from scripts.config import get_settings


def _print_receipt(receipt) -> None:
    mode = "dry-run" if receipt.dry_run else "apply"
    print(f"[reap] mode={mode} scanned={receipt.scanned} applied={len(receipt.applied)}")
    for action in receipt.actions:
        flag = "APPLIED" if action.applied else "PLANNED"
        print(
            f"  {flag} {action.action} {action.artifact_type} "
            f"{action.external_id} ({action.reason})"
        )
    if not receipt.actions:
        print("  (no expired artifacts)")


async def _run_once(apply: bool) -> int:
    from adapters.real.git_host import RealGitHost
    from services.coordination.orphans import PostgresOrphanRegistry
    from services.coordination.reaper import reap

    settings = get_settings()
    receipt = await reap(
        registry=PostgresOrphanRegistry(),
        backend=RealGitHost.from_settings(),
        allowed_repos=frozenset(allowed_repos(settings)),
        now=datetime.now(UTC),
        dry_run=not apply,
    )
    _print_receipt(receipt)
    return 0


async def _install_schedule() -> int:
    from services.orchestration.temporal_workflows.reap import ReapInput, ReapWorkflow
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
            ReapWorkflow.run,
            ReapInput(dry_run=settings.reap_schedule_dry_run),
            id=f"{settings.reap_schedule_id}-run",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(minutes=settings.reap_schedule_interval_minutes)
                )
            ]
        ),
    )
    handle = client.get_schedule_handle(settings.reap_schedule_id)
    try:
        await client.create_schedule(settings.reap_schedule_id, schedule)
        print(f"[reap] created schedule '{settings.reap_schedule_id}'")
    except Exception:
        await handle.delete()
        await client.create_schedule(settings.reap_schedule_id, schedule)
        print(f"[reap] replaced schedule '{settings.reap_schedule_id}'")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reap expired platform orphan artifacts.")
    parser.add_argument("--apply", action="store_true", help="Perform real deletions.")
    parser.add_argument(
        "--schedule", action="store_true", help="Install the Temporal reap schedule."
    )
    args = parser.parse_args()
    if args.schedule:
        return asyncio.run(_install_schedule())
    return asyncio.run(_run_once(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
