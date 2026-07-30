"""Score review-gate verdict quality against a committed gold set and emit a scorecard."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.review.eval.gold_schema import ReviewCase, read_gold_set
from services.review.types import ReviewStage

HARNESS_VERSION = "review-harness/1"
PROMPT_VERSION_TAG = "v1"

Reviewer = Callable[[str], set[str]]

RISK_MARKERS = {
    "hardcoded-secret": "password =",
    "dangerous-eval": "eval(",
    "shell-injection": "os.system(",
    "disabled-tls-verify": "verify=false",
    "destructive-migration": "drop table",
}


def keyword_baseline_reviewer(subject: str) -> set[str]:
    haystack = subject.lower()
    return {label for label, marker in RISK_MARKERS.items() if marker in haystack}


@dataclass
class StageScore:
    stage: str
    cases: int = 0
    verdict_correct: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def verdict_accuracy(self) -> float:
        return self.verdict_correct / self.cases if self.cases else 1.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "cases": self.cases,
            "verdict_accuracy": round(self.verdict_accuracy, 4),
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def score(cases: list[ReviewCase], reviewer: Reviewer) -> dict[ReviewStage, StageScore]:
    scores: dict[ReviewStage, StageScore] = {}
    for case in cases:
        stage_score = scores.setdefault(case.stage, StageScore(stage=case.stage.value))
        predicted = reviewer(case.subject)
        expected = set(case.expected_findings)
        predicted_verdict = "block" if predicted else "pass"
        stage_score.cases += 1
        if predicted_verdict == case.expected_verdict:
            stage_score.verdict_correct += 1
        stage_score.tp += len(predicted & expected)
        stage_score.fp += len(predicted - expected)
        stage_score.fn += len(expected - predicted)
    return scores


def build_scorecard(
    gold_path: Path,
    generated_at: str | None,
    cases: list[ReviewCase],
    scores: dict[ReviewStage, StageScore],
    reviewer_name: str,
) -> dict[str, Any]:
    present = [stage for stage in ReviewStage if stage in scores]
    total_cases = sum(s.cases for s in scores.values())
    total_correct = sum(s.verdict_correct for s in scores.values())
    f1s = [scores[stage].f1 for stage in present]
    try:
        gold_display = gold_path.relative_to(Path.cwd())
    except ValueError:
        gold_display = Path(gold_path.name)
    return {
        "record_type": "review_scorecard",
        "harness_version": HARNESS_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "reviewer": reviewer_name,
        "gold": {
            "path": str(gold_display),
            "cases": len(cases),
            "generated_at": generated_at,
        },
        "prompt_versions": {
            stage.value: f"{stage.value}/{PROMPT_VERSION_TAG}" for stage in present
        },
        "stages": [scores[stage].to_dict() for stage in present],
        "overall": {
            "verdict_accuracy": round(total_correct / total_cases, 4) if total_cases else 1.0,
            "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        },
    }


def run(
    gold_path: Path,
    reviewer: Reviewer = keyword_baseline_reviewer,
    reviewer_name: str = "keyword-baseline",
) -> dict[str, Any]:
    meta, cases = read_gold_set(gold_path)
    scores = score(cases, reviewer)
    generated_at = meta.generated_at if meta is not None else None
    return build_scorecard(gold_path, generated_at, cases, scores, reviewer_name)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review-gate verdict-quality eval harness")
    parser.add_argument("--gold", default=None)
    parser.add_argument("--out", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    here = Path(__file__).resolve().parent
    gold_path = (
        Path(args.gold).expanduser().resolve()
        if args.gold
        else here / "gold" / "review-gates.jsonl"
    )
    scorecard = run(gold_path)
    text = json.dumps(scorecard, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
