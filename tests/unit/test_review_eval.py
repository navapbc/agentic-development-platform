"""Tests for the review-gate eval harness."""

from __future__ import annotations

from pathlib import Path

from services.review.eval.gold_schema import GoldMeta, ReviewCase, read_gold_set, write_gold_set
from services.review.eval.harness import (
    keyword_baseline_reviewer,
    run,
    score,
)
from services.review.types import ReviewStage

_GOLD = Path(__file__).resolve().parents[2] / "services/review/eval/gold/review-gates.jsonl"


def test_gold_set_round_trip(tmp_path):
    meta = GoldMeta(generated_at="2026-07-30", baseline="keyword-baseline")
    cases = [
        ReviewCase(
            stage=ReviewStage.CODE_REVIEW,
            case_id="c1",
            subject="+os.system('x')",
            expected_verdict="block",
            expected_findings=("shell-injection",),
        ),
        ReviewCase(
            stage=ReviewStage.PLAN_REVIEW,
            case_id="p1",
            subject="add a test",
            expected_verdict="pass",
        ),
    ]
    path = tmp_path / "gold.jsonl"
    write_gold_set(path, meta, cases)
    read_meta, read_cases = read_gold_set(path)

    assert read_meta == meta
    assert read_cases == cases


def test_keyword_baseline_reviewer_is_deterministic():
    subject = "+resp = requests.get(url, verify=False)"
    assert keyword_baseline_reviewer(subject) == {"disabled-tls-verify"}
    assert keyword_baseline_reviewer(subject) == keyword_baseline_reviewer(subject)
    assert keyword_baseline_reviewer("+def add(a, b):\n+    return a + b") == set()


def test_score_counts_verdicts_and_findings():
    cases = [
        ReviewCase(
            stage=ReviewStage.CODE_REVIEW,
            case_id="tp",
            subject="+os.system('rm ' + p)",
            expected_verdict="block",
            expected_findings=("shell-injection",),
        ),
        ReviewCase(
            stage=ReviewStage.CODE_REVIEW,
            case_id="fp",
            subject="+model.eval()",
            expected_verdict="pass",
            expected_findings=(),
        ),
        ReviewCase(
            stage=ReviewStage.CODE_REVIEW,
            case_id="fn",
            subject="+open(base + user_input)",
            expected_verdict="block",
            expected_findings=("path-traversal",),
        ),
    ]
    scores = score(cases, keyword_baseline_reviewer)
    stage = scores[ReviewStage.CODE_REVIEW]

    assert stage.cases == 3
    assert stage.verdict_correct == 1
    assert stage.tp == 1
    assert stage.fp == 1
    assert stage.fn == 1
    assert stage.precision == 0.5
    assert stage.recall == 0.5
    assert stage.f1 == 0.5


def test_run_scores_committed_gold_set():
    scorecard = run(_GOLD)

    stages = {row["stage"] for row in scorecard["stages"]}
    assert stages == {
        "plan-review",
        "code-review",
        "security-review",
        "acceptance-verification",
    }
    assert scorecard["gold"]["cases"] == 12
    assert scorecard["reviewer"] == "keyword-baseline"
    assert scorecard["overall"]["verdict_accuracy"] == 0.6667
    assert scorecard["overall"]["macro_f1"] == 0.6667
    assert scorecard["prompt_versions"]["plan-review"] == "plan-review/v1"
