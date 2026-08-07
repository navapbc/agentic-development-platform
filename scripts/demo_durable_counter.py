"""Durable counter demo: watch a Temporal workflow count through a worker restart.

The workflow runs one activity per number, so the count advances on the worker,
not in this client. Restarting the worker mid-count freezes the printed count;
Temporal resumes the same workflow on the fresh worker and the count continues
from where it stopped, proving durable execution rather than a client-side loop.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import timedelta

from services.orchestration.temporal_workflows.counting_workflow import (
    CountingInput,
    CountingWorkflow,
)
from temporalio.client import Client

from scripts.config import get_settings, to_host_hostport

WORKER_SERVICE = "workflow-worker"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Durable counter demo")
    parser.add_argument("--target", type=int, default=50)
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    settings = get_settings()
    address = to_host_hostport(settings.temporal_host)
    print(f"[demo] connecting to Temporal at {address}")
    client = await Client.connect(address, namespace=settings.temporal_namespace)

    workflow_id = f"durable-counter-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        CountingWorkflow.run,
        CountingInput(target=args.target),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(seconds=settings.workflow_execution_timeout_seconds),
    )
    print(f"[demo] started {workflow_id}; counting to {args.target}, one activity per number")
    print(
        "[demo] restart the worker in another terminal at any point to simulate a crash:\n"
        f"  docker compose -f compose.yaml -f compose.override.yaml restart {WORKER_SERVICE}"
    )
    print("[demo] the count freezes while the worker is down, then resumes where it stopped\n")

    last_printed = -1
    frozen = False
    while True:
        try:
            count = await handle.query(
                CountingWorkflow.current_count, rpc_timeout=timedelta(seconds=2)
            )
        except Exception:  # noqa: BLE001
            if not frozen:
                print("  [worker down] the count is frozen; waiting for the worker to come back")
                frozen = True
            await asyncio.sleep(0.5)
            continue
        if frozen:
            print("  [worker back] the same workflow resumes")
            frozen = False
        if count != last_printed:
            print(f"  count = {count}")
            last_printed = count
        if count >= args.target:
            break
        await asyncio.sleep(0.4)

    result = await handle.result()
    print(f"\n[demo] workflow completed durably; final count = {result}")
    print("DURABLE COUNTER DEMO: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
