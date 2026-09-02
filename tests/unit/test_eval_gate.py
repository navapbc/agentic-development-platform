"""Hermetic tests for the evaluation regression gate."""

from __future__ import annotations

from services.review.eval.harness import baseline_stage_reviewer, offline_fixture_reviewer


def test_gate_passes_on_committed_inputs():
    from scripts.eval_gate import evaluate

    report = evaluate()
    assert report["review"]["kernel"] >= report["review"]["baseline"]
    assert report["context"]["ok"] is True
    assert report["ok"] is True


def test_gate_fails_when_review_regresses_below_baseline():
    from scripts.eval_gate import evaluate

    report = evaluate(
        reviewers={
            "keyword-baseline": offline_fixture_reviewer,
            "rebar-kernel": baseline_stage_reviewer,
        }
    )
    assert report["review"]["kernel"] < report["review"]["baseline"]
    assert report["review"]["ok"] is False
    assert report["ok"] is False
