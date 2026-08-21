"""Run the review-kernel comparative eval and write the scorecard.

The default run scores the rebar kernel against the keyword baseline on the
evaluation-model tier and requires Bedrock. ``--offline`` uses a deterministic
fixture in place of the kernel so the comparison reproduces without the model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.review.eval.harness import (
    baseline_stage_reviewer,
    build_rebar_reviewer,
    offline_fixture_reviewer,
    run_comparison,
)


def _default_gold() -> Path:
    return Path(__file__).resolve().parents[1] / "services/review/eval/gold/review-gates.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review-kernel comparative eval")
    parser.add_argument("--gold", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic offline fixture instead of the rebar kernel",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    gold = Path(args.gold).expanduser().resolve() if args.gold else _default_gold()
    if args.offline:
        reviewers = {
            "keyword-baseline": baseline_stage_reviewer,
            "rebar-kernel": offline_fixture_reviewer,
        }
        model_tier = "offline-fixture"
    else:
        from services.orchestration.gates.config import load_gates_config
        from services.review.kernel import RebarReviewKernel

        blocking_severities = load_gates_config().blocking_severities
        reviewers = {
            "keyword-baseline": baseline_stage_reviewer,
            "rebar-kernel": build_rebar_reviewer(
                RebarReviewKernel(blocking_severities=blocking_severities)
            ),
        }
        model_tier = "evaluation-model"

    scorecard = run_comparison(gold, reviewers, model_tier=model_tier)
    text = json.dumps(scorecard, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
