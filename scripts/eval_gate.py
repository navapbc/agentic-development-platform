"""Fail when the review kernel regresses below baseline or context accuracy regresses.

The gate is deterministic. It runs the offline review comparison and the context
consolidation over their committed inputs and fails when the review kernel drops
below the keyword baseline or any context corpus stops passing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.repository_intelligence.eval.consolidate import consolidate, load_scorecards
from services.review.eval.harness import (
    baseline_stage_reviewer,
    offline_fixture_reviewer,
    run_comparison,
)

_ROOT = Path(__file__).resolve().parents[1]
_REVIEW_GOLD = _ROOT / "services/review/eval/gold/review-gates.jsonl"
_CONTEXT_SCORECARDS = _ROOT / "services/repository_intelligence/eval/scorecards"


def evaluate(
    *,
    gold: Path = _REVIEW_GOLD,
    scorecards: Path = _CONTEXT_SCORECARDS,
    reviewers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the eval-gate report with the review and context outcomes."""
    reviewers = reviewers or {
        "keyword-baseline": baseline_stage_reviewer,
        "rebar-kernel": offline_fixture_reviewer,
    }
    comparison = run_comparison(gold, reviewers, model_tier="offline-fixture")
    overall = comparison["comparison"]["overall"]
    baseline = float(overall.get("keyword-baseline", 0.0))
    kernel = float(overall.get("rebar-kernel", 0.0))
    review_ok = kernel >= baseline

    consolidation = consolidate(load_scorecards(scorecards))
    context_ok = bool(consolidation["definition_of_done"])

    return {
        "review": {"baseline": baseline, "kernel": kernel, "ok": review_ok},
        "context": {
            "definition_of_done": context_ok,
            "repos": consolidation["summary"]["repos"],
            "ok": context_ok,
        },
        "ok": review_ok and context_ok,
    }


def main() -> int:
    report = evaluate()
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        print("[eval-gate] FAIL, an evaluation metric regressed", file=sys.stderr)
        return 1
    print("[eval-gate] PASS")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    raise SystemExit(main())
