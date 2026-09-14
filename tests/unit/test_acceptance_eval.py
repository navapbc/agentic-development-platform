"""Tests for the labeled acceptance set and the acceptance scoring harness."""

from __future__ import annotations

import pytest
from services.review.eval.acceptance_harness import (
    ADEQUACY_BAR,
    assess,
    build_review_input,
    predicted_outcome,
    score,
)
from services.review.eval.acceptance_schema import (
    AcceptanceCase,
    AcceptanceSetError,
    read_acceptance_set,
    write_acceptance_set,
)
from services.review.kernel import FakeReviewKernel
from services.review.types import (
    ModelProvenance,
    RawReview,
    ReviewInput,
    ReviewStage,
    TokenUsage,
    Verdict,
)

_ROOT_SET = "services/review/eval/acceptance_set.jsonl"


def _case(case_id: str, outcome: str) -> AcceptanceCase:
    return AcceptanceCase(
        case_id=case_id,
        acceptance_criteria="criteria",
        completion_evidence="evidence",
        outcome=outcome,
    )


class _VerdictKernel:
    """A kernel that returns a verdict chosen by the case's expected outcome label."""

    def __init__(self, by_outcome: dict[str, Verdict]) -> None:
        self._by_outcome = by_outcome
        self._cases: dict[str, str] = {}

    def run(self, review: ReviewInput) -> RawReview:
        outcome = self._cases[review.context_ref]
        return RawReview(verdict=self._by_outcome[outcome], summary="t")


def _bound_kernel(cases: list[AcceptanceCase], by_outcome: dict[str, Verdict]) -> _VerdictKernel:
    kernel = _VerdictKernel(by_outcome)
    kernel._cases = {c.case_id: c.outcome for c in cases}
    return kernel


def test_committed_set_loads_with_both_outcomes():
    cases = read_acceptance_set(_ROOT_SET)
    outcomes = {c.outcome for c in cases}
    assert outcomes == {"met", "unmet"}
    assert len(cases) >= 2


def test_missing_field_rejected(tmp_path):
    path = tmp_path / "set.jsonl"
    path.write_text('{"record_type": "case", "case_id": "x", "outcome": "met"}\n', encoding="utf-8")
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(path)


def test_bad_outcome_rejected(tmp_path):
    path = tmp_path / "set.jsonl"
    write_acceptance_set(path, [_case("x", "maybe")])
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(path)


def test_single_outcome_rejected(tmp_path):
    path = tmp_path / "set.jsonl"
    write_acceptance_set(path, [_case("a", "met"), _case("b", "met")])
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(path)


def test_duplicate_case_id_rejected(tmp_path):
    path = tmp_path / "set.jsonl"
    write_acceptance_set(path, [_case("a", "met"), _case("a", "unmet")])
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(path)


def test_predicted_outcome_only_pass_accepts():
    assert predicted_outcome(Verdict.PASS) == "met"
    assert predicted_outcome(Verdict.BLOCK) == "unmet"
    assert predicted_outcome(Verdict.INDETERMINATE) == "unmet"


def test_build_review_input_targets_acceptance_stage():
    review = build_review_input(_case("x", "met"))
    assert review.stage == ReviewStage.ACCEPTANCE_VERIFICATION
    assert review.context_ref == "x"
    assert "Acceptance criteria" in review.instructions
    assert "Completion evidence" in review.instructions


def test_scoring_perfect_agreement():
    cases = [_case("a", "met"), _case("b", "unmet")]
    kernel = _bound_kernel(cases, {"met": Verdict.PASS, "unmet": Verdict.BLOCK})
    scorecard = score(cases, kernel)
    assert scorecard.agreements == 2
    assert scorecard.false_accepts == 0
    assert scorecard.false_blocks == 0
    passed, reasons = assess(scorecard)
    assert passed
    assert reasons == []


def test_scoring_counts_false_accepts():
    cases = [_case("a", "met"), _case("b", "unmet")]
    kernel = _bound_kernel(cases, {"met": Verdict.PASS, "unmet": Verdict.PASS})
    scorecard = score(cases, kernel)
    assert scorecard.false_accepts == 1
    passed, reasons = assess(scorecard)
    assert not passed
    assert any("false accept" in r for r in reasons)


def test_scoring_counts_false_blocks():
    cases = [_case("a", "met"), _case("b", "unmet")]
    kernel = _bound_kernel(cases, {"met": Verdict.BLOCK, "unmet": Verdict.BLOCK})
    scorecard = score(cases, kernel)
    assert scorecard.false_blocks == 1
    assert scorecard.false_block_rate == 1.0
    passed, _ = assess(scorecard)
    assert not passed


def test_adequacy_bar_is_fixed_not_computed():
    assert ADEQUACY_BAR.max_false_accepts == 0
    assert ADEQUACY_BAR.min_agreement_rate == 0.80
    assert ADEQUACY_BAR.max_false_block_rate == 0.20


def test_score_runs_through_a_real_fake_kernel():
    case = _case("a", "met")
    raw = RawReview(
        verdict=Verdict.PASS,
        provenance=[ModelProvenance(leg="primary", tier="acceptance", model_id="fake")],
        usage=TokenUsage(),
        summary="ok",
    )
    scorecard = score([case], FakeReviewKernel(raw))
    assert scorecard.results[0].predicted == "met"
    assert scorecard.agreements == 1
