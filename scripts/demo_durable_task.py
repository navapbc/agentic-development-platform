"""Live demo: a durable workflow grounded in a repo briefing pack survives a worker restart."""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from datetime import timedelta

from database.models.schema import audit_events, executions
from services import process
from services.repository_intelligence.normalization.serialization import render_context_summary
from sqlalchemy import Engine, create_engine, select

from scripts.config import get_settings, to_host_hostport
from scripts.connectivity import preflight as bedrock_preflight
from scripts.db_reset import _sync_url
from scripts.durability_proof import _compose_cmd

WORKER_SERVICE = "workflow-worker"

_EVIDENCE_KEYS = (
    "symbols",
    "candidate_files",
    "callers",
    "callees",
    "related_tests",
    "dependencies",
    "recent_changes",
)

_EVIDENCE_LABELS = {
    "symbols": "key functions/classes",
    "candidate_files": "files likely to change",
    "callers": "what calls this code",
    "callees": "what this code calls",
    "related_tests": "related tests",
    "dependencies": "libraries used",
    "recent_changes": "recent changes",
}


def _execution_state(engine: Engine, execution_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(executions.c.state).where(executions.c.execution_id == execution_id)
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
            select(audit_events.c.event_type, audit_events.c.new_state, audit_events.c.model_id)
            .where(audit_events.c.execution_id == execution_id)
            .order_by(audit_events.c.created_at)
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _count(package: dict, key: str) -> int:
    value = package.get(key)
    return len(value) if isinstance(value, list) else 0


def _print_context_package(package: dict) -> None:
    print("\n[demo] briefing pack the workflow now holds safely (most relevant first):\n")
    print(render_context_summary(package))
    print("\n[demo] how many items of each kind it found:")
    for key in _EVIDENCE_KEYS:
        print(f"  {_EVIDENCE_LABELS[key]:<24} {_count(package, key)}")
    print()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", default="", help="Demo repo to ground the run with a briefing pack."
    )
    parser.add_argument(
        "--focus", default="", help="What to center the briefing pack on, e.g. a function or file."
    )
    parser.add_argument("--prompt", default="Summarize the repository in one sentence.")
    parser.add_argument(
        "--work-seconds",
        type=int,
        default=15,
        help="Length of the long-running task you interrupt with the restart.",
    )
    parser.add_argument(
        "--auto-restart",
        action="store_true",
        help="Restart the worker automatically mid-task instead of by hand (for unattended runs).",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print the full briefing pack. Act 2 shows this in detail, Act 1 leaves it off.",
    )
    return parser.parse_args()


async def main() -> int:
    from services.orchestration.temporal_workflows.durable_task_workflow import (
        DurableTaskInput,
        DurableTaskWorkflow,
    )
    from temporalio.client import Client

    args = _parse_args()
    settings = get_settings()

    ok, detail = bedrock_preflight(settings)
    print(f"[demo] preflight inference path: [{'ok' if ok else 'FAIL'}] {detail}")
    if not ok:
        print("[demo] the workflow ends with a model reply that needs the Bedrock path, aborting.")
        print("[demo] start it with `inv tunnel-start --detach`, confirm with")
        print("[demo]   `inv connectivity-check`, then retry.")
        return 2

    engine = create_engine(_sync_url(), future=True)

    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    task_id = f"demo-{uuid.uuid4().hex[:8]}"
    workflow_id = f"durable-task-{execution_id}"

    address = to_host_hostport(settings.temporal_host)
    print(f"[demo] connecting to Temporal at {address}")
    client = await Client.connect(address, namespace=settings.temporal_namespace)

    if args.repo:
        print(f"[demo] reading repository '{args.repo}' to build a briefing pack for the task")
    print(
        "[demo] starting the workflow. It saves its state, builds a briefing pack from the repo, "
        f"then runs a {args.work_seconds}s task"
    )
    handle = await client.start_workflow(
        DurableTaskWorkflow.run,
        DurableTaskInput(
            task_id=task_id,
            execution_id=execution_id,
            prompt=args.prompt,
            repo=args.repo,
            focus=args.focus,
            work_seconds=args.work_seconds,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(seconds=settings.workflow_execution_timeout_seconds),
    )

    print("[demo] waiting for the task to be received and saved...")
    if not _wait_for_state(engine, execution_id, "TASK_RECEIVED", timeout=30):
        print("[demo] FAIL: workflow did not persist TASK_RECEIVED in time")
        return 1
    print("[demo] task received and saved. The state is now safely stored in the database")

    if args.repo:
        print("[demo] the workflow is reading the repository to build its briefing pack")
        deadline = time.monotonic() + 60
        package: dict = {}
        while time.monotonic() < deadline:
            package = await handle.query(DurableTaskWorkflow.context_package)
            if package:
                break
            await asyncio.sleep(0.5)
        if not package:
            print("[demo] FAIL: workflow did not gather repository context in time")
            return 1
        if args.show_context:
            _print_context_package(package)
        else:
            print("[demo] briefing pack gathered and held durably (Act 2 shows it in full)")

    restart = " ".join([*_compose_cmd(), "restart", WORKER_SERVICE])
    print(
        f"[demo] the workflow is now running a long task, saving its progress every second.\n"
        f"[demo] restart the worker while it runs to crash it mid-task:\n  {restart}"
    )

    restarted = False
    last_step = -1
    frozen = False
    while True:
        try:
            state = await handle.query(
                DurableTaskWorkflow.progress, rpc_timeout=timedelta(seconds=2)
            )
        except Exception:  # noqa: BLE001
            if not frozen:
                print("  [worker down] the task is frozen; waiting for the worker to come back")
                frozen = True
            await asyncio.sleep(0.5)
            continue
        if frozen:
            print("  [worker back] the same workflow resumes its task where it stopped")
            frozen = False
        completed, total = state["completed"], state["total"]
        if total and completed > last_step:
            start = last_step + 1 if last_step >= 0 else completed
            for tick in range(start, completed + 1):
                print(f"  step {tick}/{total}")
            last_step = completed
        if args.auto_restart and not restarted and total and completed >= max(1, total // 3):
            print(f"[demo] auto-restarting '{WORKER_SERVICE}' to simulate a crash mid-task...")
            process.run(
                [*_compose_cmd(), "restart", WORKER_SERVICE],
                timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
                check=True,
            )
            restarted = True
        if total and completed >= total:
            break
        await asyncio.sleep(0.4)

    print("[demo] task finished. The workflow now asks the AI model, using the briefing pack")
    try:
        result = await asyncio.wait_for(handle.result(), timeout=180)
    except TimeoutError:
        print("[demo] FAIL: workflow did not complete within 180s")
        return 1

    if not _wait_for_state(engine, execution_id, "COMPLETED", timeout=30):
        print("[demo] FAIL: execution did not reach COMPLETED in the database")
        return 1

    print("\n[demo] saved history of what happened (survived the restart):")
    for event_type, new_state, model_id in _audit_trail(engine, execution_id):
        print(f"  {event_type:<20} state={new_state} model={model_id or '-'}")

    print(f"\n[demo] AI reply (grounded in the briefing pack): {result!r}")
    print("DURABLE TASK DEMO: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
