"""Reconcile Langfuse generations against usage_metrics totals per execution.

With an execution id the output reconciles that execution. Without one it
reconciles every execution with recorded usage and summarizes the result per
tenant. ``--tenant`` narrows either form to the executions recorded under that
tenant. Each execution's generations are read from its Langfuse session, and an
execution whose read stops at the page cap is reported as unverified rather than
mismatched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from database.engine import create_platform_engine
from observability import reconcile_usage
from observability.langfuse_read import fetch_generation_rows_by_session
from services.orchestration.usage_rollup import (
    BudgetCaps,
    UsageRow,
    build_rollup,
    read_usage_rows,
    rows_by_execution,
)

from scripts.config import get_settings, to_host_url

_COST_TOLERANCE = 0.01


def reconcile_execution(
    rows: Sequence[UsageRow], generations: Sequence[Mapping[str, Any]], *, caps: BudgetCaps
) -> dict[str, Any]:
    """Compare one execution's summed generations against its recorded usage totals."""
    rollup = build_rollup(rows, caps=caps)
    result = reconcile_usage(
        generations,
        prompt_tokens=rollup.totals.prompt_tokens,
        completion_tokens=rollup.totals.completion_tokens,
        cost_usd=rollup.totals.cost_usd,
        cost_tolerance=_COST_TOLERANCE,
    )
    return {
        "execution_id": rollup.execution_id,
        "tenant": rollup.tenant or None,
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
    }


def unverified_execution(rows: Sequence[UsageRow], *, caps: BudgetCaps) -> dict[str, Any]:
    """Report an execution whose Langfuse read stopped at the page cap as unverified.

    A partial read holds only some of the execution's generations, so comparing it against
    the recorded totals would report a mismatch the execution may not have.
    """
    rollup = build_rollup(rows, caps=caps)
    return {
        "execution_id": rollup.execution_id,
        "tenant": rollup.tenant or None,
        "unverified": True,
        "reason": "the Langfuse read stopped at the page cap",
        "ok": True,
    }


def tenant_summary(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Count the reconciled, mismatched, and unverified executions per tenant, by tenant."""
    summary: dict[str, dict[str, Any]] = {}
    for result in results:
        tenant = result["tenant"] or ""
        entry = summary.setdefault(
            tenant,
            {"tenant": result["tenant"], "executions": 0, "mismatched": 0, "unverified": 0},
        )
        entry["executions"] += 1
        entry["mismatched"] += 0 if result["ok"] else 1
        entry["unverified"] += 1 if result.get("unverified") else 0
    for entry in summary.values():
        entry["ok"] = entry["mismatched"] == 0
    return [summary[tenant] for tenant in sorted(summary)]


async def _run(execution_id: str, tenant: str) -> int:
    settings = get_settings()
    engine = create_platform_engine(to_host_url(settings.database_url))
    try:
        rows = await read_usage_rows(engine, execution_id, tenant=tenant)
    finally:
        await engine.dispose()

    if not rows:
        scope = f"execution {execution_id}" if execution_id else "any execution"
        suffix = f" under tenant {tenant}" if tenant else ""
        print(f"[reconcile] no usage rows for {scope}{suffix}")
        return 1

    grouped = rows_by_execution(rows)
    generations = fetch_generation_rows_by_session(
        host=to_host_url(settings.langfuse_host),
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-development"),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-development"),
        session_ids=grouped.keys(),
    )
    caps = BudgetCaps.from_settings(settings)
    results = [
        unverified_execution(execution_rows, caps=caps)
        if run_id in generations.incomplete
        else reconcile_execution(execution_rows, generations.rows.get(run_id, []), caps=caps)
        for run_id, execution_rows in grouped.items()
    ]
    ok = all(result["ok"] for result in results)
    if execution_id:
        print(json.dumps(results[0], indent=2))
    else:
        report = {
            "tenant_filter": tenant or None,
            "ok": ok,
            "tenants": tenant_summary(results),
            "executions": results,
        }
        print(json.dumps(report, indent=2))
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execution-id",
        default="",
        help="The execution id to reconcile. Omit it to reconcile every execution.",
    )
    parser.add_argument(
        "--tenant",
        default="",
        help="Reconcile only the executions recorded under this tenant. Omit it for every tenant.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.execution_id, args.tenant))


if __name__ == "__main__":
    raise SystemExit(main())
