"""Print the per-stage cost, token, and budget rollup for one execution."""

from __future__ import annotations

import argparse
import asyncio
import json

from observability.gateway import langfuse_session_url
from services.orchestration.usage_rollup import (
    BudgetCaps,
    build_rollup,
    read_usage_rows,
    rollup_to_dict,
)
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.config import get_settings, to_host_url


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
