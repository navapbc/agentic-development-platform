"""Print the cost, token, and budget rollup for one execution or for every execution.

With an execution id the output is that execution's per-stage rollup. Without one
the output groups every execution with recorded usage by tenant. ``--tenant``
narrows either form to the executions recorded under that tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable

from database.engine import create_platform_engine
from observability.gateway import langfuse_session_url
from observability.langfuse_read import (
    fetch_generation_rows_by_session,
    fetch_session_generation_rows,
)
from services.orchestration.usage_rollup import (
    BudgetCaps,
    UsageRow,
    backfill_zeroed_stages,
    build_rollup,
    build_tenant_rollups,
    execution_summary_to_dict,
    read_usage_rows,
    rollup_to_dict,
    rows_by_execution,
    tenant_rollup_to_dict,
)

from scripts.config import get_settings, to_host_url


def _public_key() -> str:
    return os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local-development")


def _secret_key() -> str:
    return os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local-development")


def _backfill_unavailable(exc: Exception) -> None:
    print(
        f"[cost-rollup] Langfuse backfill unavailable, using recorded rows: {exc}",
        file=sys.stderr,
    )


def _backfilled(settings, execution_id: str, rows: list[UsageRow]) -> list[UsageRow]:
    """Refill zeroed review-stage usage from Langfuse so the rollup stands alone.

    Falls back to the recorded rows when Langfuse is unreachable, so the rollup still
    prints offline, and reports the degradation on stderr rather than failing.
    """
    try:
        generations = fetch_session_generation_rows(
            host=to_host_url(settings.langfuse_host),
            session_id=execution_id,
            public_key=_public_key(),
            secret_key=_secret_key(),
        )
    except Exception as exc:  # noqa: BLE001
        _backfill_unavailable(exc)
        return rows
    return backfill_zeroed_stages(rows, generations)


def _backfilled_by_execution(
    settings, grouped: dict[str, list[UsageRow]]
) -> dict[str, list[UsageRow]]:
    """Refill every execution's zeroed stages from its Langfuse session.

    Falls back to the recorded rows when Langfuse is unreachable, as the single-execution
    backfill does. An execution whose read stopped at the page cap keeps its recorded rows
    and is reported on stderr, because a partial read would understate its review stages.
    """
    try:
        generations = fetch_generation_rows_by_session(
            host=to_host_url(settings.langfuse_host),
            session_ids=grouped.keys(),
            public_key=_public_key(),
            secret_key=_secret_key(),
        )
    except Exception as exc:  # noqa: BLE001
        _backfill_unavailable(exc)
        return grouped
    for execution_id in sorted(generations.incomplete):
        print(
            f"[cost-rollup] Langfuse read of {execution_id} stopped at the page cap, "
            "using its recorded rows",
            file=sys.stderr,
        )
    return {
        execution_id: rows
        if execution_id in generations.incomplete
        else backfill_zeroed_stages(rows, generations.rows.get(execution_id, []))
        for execution_id, rows in grouped.items()
    }


def _session_url(settings, execution_id: str) -> str:
    return langfuse_session_url(
        to_host_url(settings.langfuse_host), settings.langfuse_project_id, execution_id
    )


def tenant_report(
    grouped: dict[str, list[UsageRow]],
    *,
    tenant: str,
    caps: BudgetCaps,
    session_url: Callable[[str], str] = lambda _execution_id: "",
) -> dict[str, object]:
    """The multi-execution report: the tenant filter, per-tenant totals, and per-execution rows."""
    rows = [row for execution_rows in grouped.values() for row in execution_rows]
    return {
        "tenant_filter": tenant or None,
        "tenants": [tenant_rollup_to_dict(item) for item in build_tenant_rollups(rows)],
        "executions": [
            execution_summary_to_dict(
                build_rollup(
                    execution_rows,
                    caps=caps,
                    langfuse_session_url=session_url(execution_id),
                )
            )
            for execution_id, execution_rows in grouped.items()
        ],
    }


async def _run(execution_id: str, tenant: str) -> int:
    settings = get_settings()
    engine = create_platform_engine(to_host_url(settings.database_url))
    try:
        rows = await read_usage_rows(engine, execution_id, tenant=tenant)
    finally:
        await engine.dispose()

    scope = f"execution {execution_id}" if execution_id else "any execution"
    if not rows:
        suffix = f" under tenant {tenant}" if tenant else ""
        print(f"[cost-rollup] no usage rows for {scope}{suffix}")
        return 1

    caps = BudgetCaps.from_settings(settings)
    if execution_id:
        rollup = build_rollup(
            _backfilled(settings, execution_id, rows),
            caps=caps,
            langfuse_session_url=_session_url(settings, execution_id),
        )
        print(json.dumps(rollup_to_dict(rollup), indent=2))
        return 0

    grouped = _backfilled_by_execution(settings, rows_by_execution(rows))
    report = tenant_report(
        grouped,
        tenant=tenant,
        caps=caps,
        session_url=lambda run_id: _session_url(settings, run_id),
    )
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execution-id",
        default="",
        help="The execution id to roll up. Omit it to roll up every execution by tenant.",
    )
    parser.add_argument(
        "--tenant",
        default="",
        help="Roll up only the executions recorded under this tenant. Omit it for every tenant.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.execution_id, args.tenant))


if __name__ == "__main__":
    raise SystemExit(main())
