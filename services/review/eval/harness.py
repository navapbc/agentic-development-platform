"""Score review-gate verdict quality against a committed gold set and emit a scorecard."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services import process
from services.review.eval.gold_schema import ReviewCase, read_gold_set
from services.review.types import ReviewInput, ReviewStage, Verdict

HARNESS_VERSION = "review-harness/1"
COMPARISON_HARNESS_VERSION = "review-comparison-harness/1"
PROMPT_VERSION_TAG = "v1"

Reviewer = Callable[[str], set[str]]
StageReviewer = Callable[[ReviewStage, str], set[str]]

_SUBJECT_LABELS: dict[ReviewStage, str] = {
    ReviewStage.PLAN_REVIEW: "Change plan under review",
    ReviewStage.CODE_REVIEW: "Code diff under review",
    ReviewStage.SECURITY_REVIEW: "Code diff under security review",
    ReviewStage.ACCEPTANCE_VERIFICATION: "Completion evidence under acceptance verification",
}

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

    def to_verdict_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "cases": self.cases,
            "verdict_accuracy": round(self.verdict_accuracy, 4),
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


def _subject_ref(subject: str) -> str:
    return f"sha256:{hashlib.sha256(subject.encode('utf-8')).hexdigest()}"


def baseline_stage_reviewer(stage: ReviewStage, subject: str) -> set[str]:
    return keyword_baseline_reviewer(subject)


def offline_fixture_reviewer(stage: ReviewStage, subject: str) -> set[str]:
    """A deterministic stand-in for the kernel used by the harness ``--offline`` mode.

    It extends the keyword baseline with a path-traversal heuristic so a comparison
    can run without the evaluation model, for example in CI. The live comparison
    uses the rebar kernel instead.
    """
    labels = set(keyword_baseline_reviewer(subject))
    if "open(" in subject.lower() and "+" in subject:
        labels.add("path-traversal")
    return labels


def build_rebar_reviewer(kernel: Any) -> StageReviewer:
    """Adapt a review kernel to the stage-aware reviewer seam for the eval harness."""

    def reviewer(stage: ReviewStage, subject: str) -> set[str]:
        from services.prompts import load_prompt

        loaded = load_prompt(stage.value)
        instructions = f"{_SUBJECT_LABELS[stage]}:\n{subject}"
        review_input = ReviewInput(
            stage=stage,
            subject_ref=_subject_ref(subject),
            context_ref="",
            prompt_name=stage.value,
            prompt_version=loaded.version,
            system_prompt=loaded.text,
            instructions=instructions,
            source="local",
        )
        raw = kernel.run(review_input)
        if raw.verdict != Verdict.BLOCK:
            return set()
        titles = {f.title or f.finding_id for f in raw.findings if f.blocking}
        return titles or {"blocking"}

    return reviewer


def score_stage_aware(
    cases: list[ReviewCase], reviewer: StageReviewer
) -> dict[ReviewStage, StageScore]:
    scores: dict[ReviewStage, StageScore] = {}
    for case in cases:
        stage_score = scores.setdefault(case.stage, StageScore(stage=case.stage.value))
        predicted = reviewer(case.stage, case.subject)
        expected = set(case.expected_findings)
        predicted_verdict = "block" if predicted else "pass"
        stage_score.cases += 1
        if predicted_verdict == case.expected_verdict:
            stage_score.verdict_correct += 1
        stage_score.tp += len(predicted & expected)
        stage_score.fp += len(predicted - expected)
        stage_score.fn += len(expected - predicted)
    return scores


def _git_commit() -> str | None:
    try:
        result = process.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _gold_sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _gate_config_hash() -> str | None:
    try:
        from services.orchestration.gates.config import load_gates_config
        from services.orchestration.gates.manifest import gate_config_hash

        return gate_config_hash(load_gates_config())
    except Exception:  # noqa: BLE001
        return None


def _library_prompt_versions() -> dict[str, str]:
    from services.prompts.library import REVIEW_PROMPTS

    return {name: definition.version for name, definition in REVIEW_PROMPTS.items()}


def _reviewer_report(
    scores: dict[ReviewStage, StageScore], *, include_labels: bool
) -> dict[str, Any]:
    present = [stage for stage in ReviewStage if stage in scores]
    total_cases = sum(s.cases for s in scores.values())
    total_correct = sum(s.verdict_correct for s in scores.values())
    verdict_accuracy = round(total_correct / total_cases, 4) if total_cases else 1.0
    if include_labels:
        f1s = [scores[stage].f1 for stage in present]
        return {
            "labels_scored": True,
            "stages": [scores[stage].to_dict() for stage in present],
            "overall": {
                "verdict_accuracy": verdict_accuracy,
                "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
            },
        }
    return {
        "labels_scored": False,
        "stages": [scores[stage].to_verdict_dict() for stage in present],
        "overall": {"verdict_accuracy": verdict_accuracy},
    }


def run_comparison(
    gold_path: Path,
    reviewers: dict[str, StageReviewer],
    *,
    model_tier: str,
    label_reviewers: Iterable[str] = ("keyword-baseline",),
) -> dict[str, Any]:
    meta, cases = read_gold_set(gold_path)
    names = list(reviewers.keys())
    label_set = set(label_reviewers)
    scored = {name: score_stage_aware(cases, reviewer) for name, reviewer in reviewers.items()}
    reports = {
        name: _reviewer_report(scored[name], include_labels=name in label_set) for name in names
    }

    per_stage: list[dict[str, Any]] = []
    for stage in ReviewStage:
        if not any(stage in scored[name] for name in names):
            continue
        row: dict[str, Any] = {"stage": stage.value}
        for name in names:
            stage_score = scored[name].get(stage)
            row[name] = round(stage_score.verdict_accuracy, 4) if stage_score else None
        if len(names) == 2 and row[names[0]] is not None and row[names[1]] is not None:
            row["delta"] = round(row[names[1]] - row[names[0]], 4)
        per_stage.append(row)

    overall = {name: reports[name]["overall"]["verdict_accuracy"] for name in names}
    if len(names) == 2:
        overall["delta"] = round(overall[names[1]] - overall[names[0]], 4)

    try:
        gold_display = gold_path.relative_to(Path.cwd())
    except ValueError:
        gold_display = Path(gold_path.name)

    return {
        "record_type": "review_comparison_scorecard",
        "harness_version": COMPARISON_HARNESS_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": _git_commit(),
        "model_tier": model_tier,
        "metric": "verdict_accuracy",
        "gold": {
            "path": str(gold_display),
            "cases": len(cases),
            "generated_at": meta.generated_at if meta is not None else None,
            "sha256": _gold_sha256(gold_path),
        },
        "prompt_versions": _library_prompt_versions(),
        "gate_config_hash": _gate_config_hash(),
        "reviewers": reports,
        "comparison": {"per_stage": per_stage, "overall": overall},
    }


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
