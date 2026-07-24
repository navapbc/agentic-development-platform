"""Phase-1 durability proof: a Temporal workflow survives a worker restart."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid

from database.models import SCHEMA
from sqlalchemy import Engine, create_engine, text

from scripts.config import get_settings, to_host_hostport
from scripts.db_reset import _sync_url

WORKER_SERVICE = "workflow-worker"
PAUSE_SECONDS = 25


def _compose_cmd() -> list[str]:
    files = ["-f", "compose.yaml"]
    if os.path.exists("compose.override.yaml"):
        files += ["-f", "compose.override.yaml"]
    return ["docker", "compose", *files]


def _execution_state(engine: Engine, execution_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(f'SELECT state FROM "{SCHEMA}".executions WHERE execution_id = :eid'),
            {"eid": execution_id},
        ).first()
    return row[0] if row else None


def _wait_for_state(engine: Engine, execution_id: str, target: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _execution_state(engine, execution_id) == target:
            return True
        time.sleep(1.0)
    return False


def _audit_trail(engine: Engine, execution_id: str) -> list[tuple[str, str | None, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f'SELECT event_type, new_state, model_id FROM "{SCHEMA}".audit_events '
                "WHERE execution_id = :eid ORDER BY created_at"
            ),
            {"eid": execution_id},
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def main() -> int:
    from services.orchestration.temporal_workflows.foundation import (
        FoundationInput,
        FoundationWorkflow,
    )
    from temporalio.client import Client

    settings = get_settings()
    engine = create_engine(_sync_url(), future=True)

    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    task_id = f"proof-{uuid.uuid4().hex[:8]}"
    workflow_id = f"foundation-{execution_id}"

    address = to_host_hostport(settings.temporal_host)
    print(f"[proof] connecting to Temporal at {address}")
    client = await Client.connect(address, namespace=settings.temporal_namespace)

    print(f"[proof] starting {workflow_id} with a {PAUSE_SECONDS}s durable pause")
    handle = await client.start_workflow(
        FoundationWorkflow.run,
        FoundationInput(
            task_id=task_id,
            execution_id=execution_id,
            prompt="Reply with the single word: ok",
            pause_seconds=PAUSE_SECONDS,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    print("[proof] waiting for TASK_RECEIVED to persist before the restart...")
    if not _wait_for_state(engine, execution_id, "TASK_RECEIVED", timeout=30):
        print("[proof] FAIL: workflow did not persist TASK_RECEIVED in time")
        return 1
    print("[proof] TASK_RECEIVED persisted; workflow is parked in a durable timer")

    print(f"[proof] restarting '{WORKER_SERVICE}' to simulate a worker crash...")
    subprocess.run([*_compose_cmd(), "restart", WORKER_SERVICE], check=True)
    print("[proof] worker restarted; waiting for the workflow to resume and complete")

    try:
        result = await asyncio.wait_for(handle.result(), timeout=120)
    except TimeoutError:
        print("[proof] FAIL: workflow did not complete within 120s of the restart")
        return 1

    if not _wait_for_state(engine, execution_id, "COMPLETED", timeout=30):
        print("[proof] FAIL: execution did not reach COMPLETED in the database")
        return 1

    print("\n[proof] durable audit trail (survived the restart):")
    for event_type, new_state, model_id in _audit_trail(engine, execution_id):
        print(f"  {event_type:<20} state={new_state} model={model_id or '-'}")

    print(f"\n[proof] workflow result: {result!r}")
    print("DURABILITY PROOF: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
