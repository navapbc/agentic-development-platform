"""Print the per-stage cost, token, and budget rollup for one execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from observability.gateway import langfuse_session_url
from observability.langfuse_read import fetch_session_generation_rows
from services.orchestration.usage_rollup import (
    BudgetCaps,
    backfill_zeroed_stages,
    build_rollup,
    read_usage_rows,
    rollup_to_dict,
)
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.config import get_settings, to_host_url


def _backfilled(settings, execution_id, rows):
    """Refill zeroed review-stage usage from Langfuse so the rollup stands alone.

    Falls back to the recorded rows when Langfuse is unreachable, so the rollup still
    prints offline, and reports the degradation on stderr rather than failing.
    """
    try:
        generations = fetch_session_generation_rows(
            host=to_host_url(settings.langfuse_host),
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-development"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-development"),
            session_id=execution_id,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to recorded rows, never fail the rollup
        print(
            f"[cost-rollup] Langfuse backfill unavailable, using recorded rows: {exc}",
            file=sys.stderr,
        )
        return rows
    return backfill_zeroed_stages(rows, generations)


async def _run(execution_id: str) -> int:
    settings = get_settings()
    engine = create_async_engine(
        to_host_url(settings.database_url), future=True, pool_pre_ping=True
    )
    try:
        rows = await read_usage_rows(engine, execution_id)
    finally:
        await engine.dispose()

    if not rows:
        print(f"[cost-rollup] no usage rows for execution {execution_id}")
        return 1

    rows = _backfilled(settings, execution_id, rows)
    session_url = langfuse_session_url(
        to_host_url(settings.langfuse_host), settings.langfuse_project_id, execution_id
    )
    rollup = build_rollup(
        rows, caps=BudgetCaps.from_settings(settings), langfuse_session_url=session_url
    )
    print(json.dumps(rollup_to_dict(rollup), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-id", required=True, help="The execution id to roll up.")
    args = parser.parse_args()
    return asyncio.run(_run(args.execution_id))


if __name__ == "__main__":
    raise SystemExit(main())
