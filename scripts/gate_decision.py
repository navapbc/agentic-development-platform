"""Approve or reject a workflow parked at a configured human intervention gate."""

from __future__ import annotations

import argparse
import asyncio


async def _decide(issue_key: str, *, gate: str, approved: bool, actor: str) -> int:
    from services.orchestration.gate_decision import run_gate_decision

    result = await run_gate_decision(issue_key, gate=gate, approved=approved, actor=actor)
    print(f"[gate] {result.message}")
    return 0 if result.resumed else 1


def main() -> int:
    from scripts.operator_identity import local_operator

    parser = argparse.ArgumentParser(
        description="Resume a workflow parked at a configured human intervention gate."
    )
    parser.add_argument(
        "issue", help="Jira issue key parked at a human gate, for example ADSER-24."
    )
    parser.add_argument(
        "--gate", required=True, help="Name of the human gate to resolve, for example plan."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--approve", action="store_true", help="Approve the gate and continue the run."
    )
    group.add_argument(
        "--reject", action="store_true", help="Reject the gate and cancel the run."
    )
    args = parser.parse_args()
    return asyncio.run(
        _decide(args.issue, gate=args.gate, approved=args.approve, actor=local_operator())
    )


if __name__ == "__main__":
    raise SystemExit(main())
