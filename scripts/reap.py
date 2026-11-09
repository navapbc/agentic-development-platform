"""Orphan reaper runner: local dry-run inspection, apply, or Temporal Schedule installation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from services.integration.source_control import source_control_repos

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
        allowed_repos=frozenset(source_control_repos(settings)),
        now=datetime.now(UTC),
        dry_run=not apply,
    )
    _print_receipt(receipt)
    return 0


async def install_schedule(client: Any = None) -> int:
    """Register the reap schedule of this process's tenant and remove the untenanted one."""
    from services import temporal_client
    from services.orchestration.task_queues import run_task_queue, tenant_schedule_id
    from services.orchestration.temporal_workflows.reap import ReapInput, ReapWorkflow
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        SchedulePolicy,
        ScheduleSpec,
    )
    from temporalio.service import RPCError, RPCStatusCode

    settings = get_settings()
    if client is None:
        client = await temporal_client.connect(settings.temporal_host, settings)
    schedule_id = tenant_schedule_id(settings.reap_schedule_id, settings)
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            ReapWorkflow.run,
            ReapInput(dry_run=settings.reap_schedule_dry_run),
            id=f"{schedule_id}-run",
            task_queue=run_task_queue(settings),
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(minutes=settings.reap_schedule_interval_minutes)
                )
            ]
        ),
        # A run missed while Temporal was down, as the off-hours schedule leaves it, starts at
        # most once on wake instead of once for every missed interval.
        policy=SchedulePolicy(
            catchup_window=timedelta(minutes=settings.reap_schedule_interval_minutes)
        ),
    )
    handle = client.get_schedule_handle(schedule_id)
    try:
        await client.create_schedule(schedule_id, schedule)
        print(f"[reap] created schedule '{schedule_id}'")
    except Exception:
        await handle.delete()
        await client.create_schedule(schedule_id, schedule)
        print(f"[reap] replaced schedule '{schedule_id}'")
    if schedule_id != settings.reap_schedule_id:
        try:
            await client.get_schedule_handle(settings.reap_schedule_id).delete()
            print(f"[reap] removed schedule '{settings.reap_schedule_id}'")
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reap expired platform orphan artifacts.")
    parser.add_argument("--apply", action="store_true", help="Perform real deletions.")
    parser.add_argument(
        "--schedule", action="store_true", help="Install the Temporal reap schedule."
    )
    args = parser.parse_args()
    if args.schedule:
        return asyncio.run(install_schedule())
    return asyncio.run(_run_once(args.apply))


if __name__ == "__main__":
    from services.temporal_client import exit_without_finalization

    exit_without_finalization(main())
