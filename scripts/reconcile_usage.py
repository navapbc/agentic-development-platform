"""Reconcile an execution's Langfuse generations against its usage_metrics totals."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from observability import reconcile_usage
from observability.langfuse_read import fetch_session_generations
from services.orchestration.usage_rollup import BudgetCaps, build_rollup, read_usage_rows
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.config import get_settings, to_host_url

_COST_TOLERANCE = 0.01


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
        print(f"[reconcile] no usage rows for execution {execution_id}")
        return 1

    rollup = build_rollup(rows, caps=BudgetCaps.from_settings(settings))
    generations = fetch_session_generations(
        host=to_host_url(settings.langfuse_host),
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-development"),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-development"),
        session_id=execution_id,
    )
    result = reconcile_usage(
        generations,
        prompt_tokens=rollup.totals.prompt_tokens,
        completion_tokens=rollup.totals.completion_tokens,
        cost_usd=rollup.totals.cost_usd,
        cost_tolerance=_COST_TOLERANCE,
    )
    print(
        json.dumps(
            {
                "execution_id": execution_id,
                "langfuse_generations": len(generations),
                "usage_metrics": {
                    "prompt_tokens": rollup.totals.prompt_tokens,
                    "completion_tokens": rollup.totals.completion_tokens,
                    "cost_usd": round(rollup.totals.cost_usd, 6),
                },
                "ok": result.ok,
                "prompt_token_diff": result.prompt_token_diff,
                "completion_token_diff": result.completion_token_diff,
                "cost_diff": round(result.cost_diff, 6),
            },
            indent=2,
        )
    )
    return 0 if result.ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-id", required=True, help="The execution id to reconcile.")
    args = parser.parse_args()
    return asyncio.run(_run(args.execution_id))


if __name__ == "__main__":
    raise SystemExit(main())
