"""Narrated coordination demo: real snapshot, real claims, a live race the database decides."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import uuid
from datetime import UTC, datetime

from database.models.schema import (
    audit_events,
    executions,
    side_effects,
    task_snapshots,
    work_claims,
)
from services.coordination import (
    ADMITTED,
    DISCARDED,
    DeterminismManifest,
    PostgresManifestStore,
    PostgresSequenceGate,
    PostgresSideEffectLedger,
    PostgresWorkClaimStore,
    SequencedAction,
    SideEffectRef,
    WorkUnit,
    partition_by_file_impact,
)
from services.integration.jira.adapter import build_jira_adapter
from services.integration.jira.snapshot import (
    AllowlistError,
    PostgresTaskSnapshotStore,
    capture_task_snapshot,
)
from sqlalchemy import Select, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from scripts.config import Settings, get_settings, to_host_url


def _fact(text: str) -> None:
    print(f"  • {text}")


def _clock(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%H:%M:%S.%f")[:-3]


def _authed_remote(settings: Settings, repo: str) -> str:
    base = settings.github_git_base_url.rstrip("/")
    if settings.github_token and base.startswith("https://"):
        base = f"https://x-access-token:{settings.github_token}@{base[len('https://') :]}"
    return f"{base}/{settings.github_owner}/{repo}.git"


def _head_sha(remote: str) -> str:
    proc = subprocess.run(
        ["git", "ls-remote", remote, "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


def _fetch_issue(settings: Settings, task_id: str) -> tuple[dict | None, str, str]:
    try:
        adapter = build_jira_adapter(settings)
        raw = adapter.get_issue(task_id)
    except Exception as exc:  # noqa: BLE001
        return None, "", f"could not read {task_id} from Jira ({exc})"
    return raw, adapter.source, ""


async def _db_ready(engine: AsyncEngine) -> bool:
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def _seed_execution(engine: AsyncEngine, execution_id: str, task_id: str, repo: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(executions)
            .values(
                execution_id=execution_id,
                task_id=task_id,
                state="IMPLEMENTING",
                repository=repo,
            )
            .on_conflict_do_nothing(index_elements=[executions.c.execution_id])
        )


async def _count(engine: AsyncEngine, stmt: Select) -> int:
    async with engine.begin() as conn:
        return len((await conn.execute(stmt)).all())


def _action(gate_scope: str, execution_id: str, agent_id: str, sequence: int) -> SequencedAction:
    return SequencedAction(
        task_id=gate_scope,
        execution_id=execution_id,
        sequence=sequence,
        idempotency_key=f"{agent_id}:{sequence}",
        operation="jira_comment",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Live coordination demo against the real stack.")
    parser.add_argument("--task", default="ADSER-13", help="Real Jira ticket to snapshot.")
    parser.add_argument("--repo", default="agentic-demo-service", help="Repo whose head to pin.")
    parser.add_argument("--pace", type=float, default=0.6, help="Pause between steps, seconds.")
    parser.add_argument("--race-agents", type=int, default=5, help="Agents racing for one unit.")
    args = parser.parse_args()

    settings = get_settings()
    task_id = args.task
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(
        to_host_url(settings.database_url), future=True, pool_pre_ping=True
    )

    print(f"[demo] run id {execution_id}  ticket {task_id}  {_clock()}")

    if not await _db_ready(engine):
        print("[demo] cannot reach Postgres, bring the stack up first by running inv local-up")
        await engine.dispose()
        return 2

    raw_issue, jira_source, err = _fetch_issue(settings, task_id)
    if raw_issue is None:
        print(f"[demo] {err}")
        print("[demo] set Jira Cloud credentials and pass a readable ticket with --task.")
        await engine.dispose()
        return 2

    try:
        repository_sha = _head_sha(_authed_remote(settings, args.repo))
    except Exception as exc:  # noqa: BLE001
        print(f"[demo] could not read the head commit of {args.repo} because {exc}")
        await engine.dispose()
        return 2
    if not repository_sha:
        print(f"[demo] {args.repo} has no head commit to pin.")
        await engine.dispose()
        return 2

    await _seed_execution(engine, execution_id, task_id, args.repo)

    print(f"\n[1/6] Lock the ticket, snapshot {task_id} so it cannot change mid-run")
    try:
        snapshot = await capture_task_snapshot(
            raw_issue,
            execution_id=execution_id,
            jira_source=jira_source,
            settings=settings,
            store=PostgresTaskSnapshotStore(engine),
        )
    except AllowlistError as exc:
        print(f"[demo] {exc}")
        await engine.dispose()
        return 2
    _fact(f"captured snapshot version {snapshot.version} of {task_id}")
    _fact(f"content fingerprint {snapshot.content_hash[:16]} computed from the ticket contents")
    _fact(f"saved to Postgres as task_snapshots row {snapshot.snapshot_id[:16]}")
    await asyncio.sleep(args.pace)

    print("\n[2/6] Pin the inputs, record the exact commit behind this run")
    await PostgresManifestStore(engine).record(
        DeterminismManifest(
            execution_id=execution_id,
            task_id=task_id,
            snapshot_version=snapshot.version,
            repository_sha=repository_sha,
        )
    )
    _fact(f"recorded {args.repo} at commit {repository_sha[:12]} as the code for this run")
    await asyncio.sleep(args.pace)

    print("\n[3/6] Schedule in parallel only when work touches different files")
    unit_a = WorkUnit(
        task_id, f"{execution_id}:client", execution_id, "agent-a", ("src/client.py",)
    )
    unit_b = WorkUnit(task_id, f"{execution_id}:retry", execution_id, "agent-b", ("src/retry.py",))
    collider = WorkUnit(
        task_id, f"{execution_id}:client-2", execution_id, "agent-c", ("src/client.py",)
    )
    schedule = partition_by_file_impact([unit_a, unit_b, collider])
    _fact(f"{len(schedule.admitted)} units run in parallel on src/client.py and src/retry.py")
    _fact(f"{len(schedule.deferred)} unit waits because src/client.py is being changed by agent-a")
    await asyncio.sleep(args.pace)

    print(
        f"\n[4/6] Work lock, {args.race_agents} agents race for one unit and the database picks one"
    )
    claims = PostgresWorkClaimStore(engine)
    contested_unit = f"{execution_id}:contested"
    racers = [
        WorkUnit(task_id, contested_unit, execution_id, f"racer-{i}", ("src/config.py",))
        for i in range(args.race_agents)
    ]
    outcomes = await asyncio.gather(*(claims.try_claim(unit) for unit in racers))
    winners = [outcome for outcome in outcomes if outcome.won]
    race_claim_id = winners[0].claim_id if winners else ""
    won_row = None
    if race_claim_id:
        async with engine.begin() as conn:
            won_row = (
                await conn.execute(
                    select(
                        work_claims.c.agent_id,
                        work_claims.c.claim_version,
                        work_claims.c.created_at,
                    ).where(work_claims.c.claim_id == race_claim_id)
                )
            ).first()
    rows_for_unit = await _count(
        engine,
        select(work_claims.c.claim_id).where(work_claims.c.claim_id == race_claim_id),
    )
    losers = len(outcomes) - len(winners)
    _fact("every agent goes for the same change to src/config.py, but only one can hold the lock")
    _fact(f"{len(outcomes)} agents raced, {len(winners)} won and {losers} were turned away")
    if won_row is not None:
        _fact(f"claim {race_claim_id[:16]} won by {won_row.agent_id}")
        _fact(f"granted as claim version {won_row.claim_version} at {_clock(won_row.created_at)}")
    _fact(f"exactly {rows_for_unit} claim row exists for that unit")
    both = await asyncio.gather(claims.try_claim(unit_a), claims.try_claim(unit_b))
    both_won = sum(1 for outcome in both if outcome.won)
    _fact(f"{both_won} units on separate files, src/client.py and src/retry.py, were both claimed")
    await asyncio.sleep(args.pace)

    print("\n[5/6] Ordering gate, a retried step that arrives too late is thrown out, not applied")
    gate = PostgresSequenceGate(engine)
    gate_scope = f"{task_id}:{execution_id}"
    await gate.admit(_action(gate_scope, execution_id, "agent-a", 1))
    await gate.admit(_action(gate_scope, execution_id, "agent-b", 2))
    await gate.admit(_action(gate_scope, execution_id, "agent-b", 3))
    _fact("steps 1, 2 and 3 arrived in order and were applied, so the gate is now past step 3")
    stale = await gate.admit(_action(gate_scope, execution_id, "agent-a", 1))
    _fact(f"step 1 is retried after 1, 2 and 3, so the gate {stale.outcome} the stale retry")
    receipts = await _count(
        engine,
        select(audit_events.c.event_id).where(
            audit_events.c.execution_id == execution_id,
            audit_events.c.event_type == "side_effect_discarded",
        ),
    )
    _fact(f"{receipts} discard receipt written to the audit trail so the skip can be reviewed")
    await asyncio.sleep(args.pace)

    print("\n[6/6] Retry safety, a repeated step never sends a second outside write")
    ledger = PostgresSideEffectLedger(engine)
    calls = {"n": 0}

    async def _post_effect() -> dict:
        calls["n"] += 1
        return {"comment_id": f"c-{execution_id}"}

    ref = SideEffectRef(task_id, execution_id, "jira_comment", task_id)
    await ledger.run_once(ref, _post_effect)
    replayed = await ledger.run_once(ref, _post_effect)
    completed = await _count(
        engine,
        select(side_effects.c.side_effect_key).where(
            side_effects.c.execution_id == execution_id,
            side_effects.c.status == "completed",
        ),
    )
    _fact("the first attempt ran the write, the retry found it already done")
    _fact(f"so only {calls['n']} outside write was made, the retry made none")
    _fact(f"{completed} completed record in the ledger that marks the write as done")
    await asyncio.sleep(args.pace)

    snapshot_rows = await _count(
        engine,
        select(task_snapshots.c.snapshot_id).where(task_snapshots.c.execution_id == execution_id),
    )
    claim_rows = await _count(
        engine,
        select(work_claims.c.claim_id).where(work_claims.c.execution_id == execution_id),
    )
    print(f"\n[db] live rows this run wrote to agent_platform, keyed to {execution_id}")
    _fact(f"{snapshot_rows} row in task_snapshots")
    _fact(f"{claim_rows} rows in work_claims")
    _fact(f"{receipts} discard receipt in audit_events")
    _fact(f"{completed} completed row in side_effects")

    follow_up = await gate.admit(_action(gate_scope, execution_id, "agent-b", 4))
    ok = (
        len(winners) == 1
        and rows_for_unit == 1
        and both_won == 2
        and stale.outcome == DISCARDED
        and follow_up.outcome == ADMITTED
        and replayed.replayed
        and calls["n"] == 1
        and completed == 1
    )
    print("\nCOORDINATION DEMO: " + ("PASS" if ok else "FAIL"))
    await engine.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
