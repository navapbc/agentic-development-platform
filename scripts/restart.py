"""Abandon a parked workflow and re-trigger Jira intake for a fresh execution."""

from __future__ import annotations

import argparse
import asyncio


async def _restart(issue_key: str, *, actor: str, repo: str | None) -> int:
    from services.orchestration.task_restart import run_restart_task

    result = await run_restart_task(issue_key, actor=actor, repo=repo)
    print(f"[restart] {result.message}")
    return 0 if result.restarted else 1


def main() -> int:
    from scripts.operator_identity import local_operator

    parser = argparse.ArgumentParser(
        description="Abandon a parked workflow and re-trigger intake for a fresh execution."
    )
    parser.add_argument(
        "issue", help="Jira issue key to restart, for example ADSER-24."
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Scope the intake retry to one repository when the issue fanned out to several.",
    )
    args = parser.parse_args()
    return asyncio.run(_restart(args.issue, actor=local_operator(), repo=args.repo))


if __name__ == "__main__":
    raise SystemExit(main())
