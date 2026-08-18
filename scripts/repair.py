"""Retry or abandon a workflow parked for human repair after CI failure."""

from __future__ import annotations

import argparse
import asyncio


async def _decide(issue_key: str, *, retry: bool, actor: str) -> int:
    from services.orchestration.repair_decision import run_repair_decision

    result = await run_repair_decision(issue_key, retry=retry, actor=actor)
    print(f"[repair] {result.message}")
    return 0 if result.resumed else 1


def main() -> int:
    from scripts.operator import local_operator

    parser = argparse.ArgumentParser(
        description="Resume a workflow parked for human repair after CI failure."
    )
    parser.add_argument(
        "issue", help="Jira issue key parked for human repair, for example ADSER-24."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--retry",
        action="store_true",
        help="Re-run CI and repair on the current branch head.",
    )
    group.add_argument(
        "--abandon",
        action="store_true",
        help="Abandon the workflow so intake can relaunch a fresh execution.",
    )
    args = parser.parse_args()
    return asyncio.run(_decide(args.issue, retry=args.retry, actor=local_operator()))


if __name__ == "__main__":
    raise SystemExit(main())
