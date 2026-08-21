"""Tests for the review-gate eval harness."""

from __future__ import annotations

from pathlib import Path

from services.review.eval.gold_schema import GoldMeta, ReviewCase, read_gold_set, write_gold_set
from services.review.eval.harness import (
    baseline_stage_reviewer,
    build_rebar_reviewer,
    keyword_baseline_reviewer,
    run,
    run_comparison,
    score,
)
from services.review.types import Finding, RawReview, ReviewStage, Verdict

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
    assert scorecard["gold"]["cases"] == 20
    assert scorecard["reviewer"] == "keyword-baseline"
    assert 0.0 <= scorecard["overall"]["verdict_accuracy"] <= 1.0
    assert 0.0 <= scorecard["overall"]["macro_f1"] <= 1.0
    assert scorecard["prompt_versions"]["plan-review"] == "plan-review/v1"


class _BlockingKernel:
    """Deterministic review-kernel double that blocks on a shell-injection marker."""

    def run(self, review):
        blocks = "os.system(" in review.instructions
        findings = (
            [Finding("f1", "high", True, "shell-injection", "")] if blocks else []
        )
        verdict = Verdict.BLOCK if blocks else Verdict.PASS
        return RawReview(verdict=verdict, findings=findings)


def test_build_rebar_reviewer_maps_the_kernel_verdict():
    from services.prompts import StaticPromptStore, set_store

    set_store(StaticPromptStore())
    try:
        reviewer = build_rebar_reviewer(_BlockingKernel())
        assert reviewer(ReviewStage.CODE_REVIEW, "+os.system('rm ' + p)")
        assert reviewer(ReviewStage.CODE_REVIEW, "+def add(a, b):\n+    return a + b") == set()
    finally:
        set_store(None)


def test_run_comparison_scores_both_reviewers_over_the_gold_set():
    from services.prompts import StaticPromptStore, set_store

    set_store(StaticPromptStore())
    try:
        card = run_comparison(
            _GOLD,
            {
                "keyword-baseline": baseline_stage_reviewer,
                "rebar-kernel": build_rebar_reviewer(_BlockingKernel()),
            },
            model_tier="test",
        )
    finally:
        set_store(None)

    assert card["record_type"] == "review_comparison_scorecard"
    assert set(card["reviewers"]) == {"keyword-baseline", "rebar-kernel"}
    assert card["gold"]["cases"] == 20
    assert card["gold"]["sha256"].startswith("sha256:")
    assert card["gate_config_hash"] is None or card["gate_config_hash"].startswith("sha256:")
    assert card["prompt_versions"]["plan-review"] == "plan-review/v1"
    stages = {row["stage"] for row in card["comparison"]["per_stage"]}
    assert stages == {
        "plan-review",
        "code-review",
        "security-review",
        "acceptance-verification",
    }
    assert "delta" in card["comparison"]["overall"]


def test_committed_comparison_scorecard_is_wellformed():
    import json

    path = (
        Path(__file__).resolve().parents[2]
        / "services/review/eval/scorecards/review-comparison.2026-08-20.json"
    )
    card = json.loads(path.read_text(encoding="utf-8"))
    assert card["record_type"] == "review_comparison_scorecard"
    assert card["model_tier"] == "evaluation-model"
    assert card["metric"] == "verdict_accuracy"
    assert set(card["reviewers"]) == {"keyword-baseline", "rebar-kernel"}
    assert card["reviewers"]["keyword-baseline"]["labels_scored"] is True
    assert card["reviewers"]["rebar-kernel"]["labels_scored"] is False
    assert card["gold"]["sha256"].startswith("sha256:")
    assert card["gate_config_hash"].startswith("sha256:")
    assert card["prompt_versions"]["plan-review"] == "plan-review/v1"
    stages = {row["stage"] for row in card["comparison"]["per_stage"]}
    assert stages == {
        "plan-review",
        "code-review",
        "security-review",
        "acceptance-verification",
    }
    assert "delta" in card["comparison"]["overall"]
