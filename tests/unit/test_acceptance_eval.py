"""Tests for the grounded acceptance set and the acceptance scoring harness."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

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
    materialize_case,
    read_acceptance_set,
    snapshot_case,
)
from services.review.types import RawReview, ReviewInput, ReviewStage, Verdict

_ROOT_SET = "services/review/eval/acceptance_cases"
_HAS_GIT = shutil.which("git") is not None
requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git must be on PATH")


def _write_case(root: Path, case_id: str, outcome: str, *, base: str, head: str) -> None:
    d = root / case_id
    (d / "base").mkdir(parents=True)
    (d / "head").mkdir(parents=True)
    (d / "case.json").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "acceptance_criteria": "criteria",
                "change_summary": "summary",
                "outcome": outcome,
            }
        ),
        encoding="utf-8",
    )
    (d / "base" / "f.py").write_text(base, encoding="utf-8")
    (d / "head" / "f.py").write_text(head, encoding="utf-8")


def _one(case_dir: Path) -> AcceptanceCase:
    from services.review.eval.acceptance_schema import _parse_case

    return _parse_case(case_dir)


def test_committed_set_loads_with_both_outcomes():
    cases = read_acceptance_set(_ROOT_SET)
    outcomes = {c.outcome for c in cases}
    assert outcomes == {"met", "unmet"}
    assert len(cases) >= 2
    for c in cases:
        assert c.changed_files(), f"{c.case_id} has no diff"


def test_missing_field_rejected(tmp_path):
    d = tmp_path / "c1"
    (d / "head").mkdir(parents=True)
    (d / "case.json").write_text(json.dumps({"case_id": "c1", "outcome": "met"}), encoding="utf-8")
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(tmp_path)


def test_bad_outcome_rejected(tmp_path):
    _write_case(tmp_path, "c1", "maybe", base="a\n", head="b\n")
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(tmp_path)


def test_single_outcome_rejected(tmp_path):
    _write_case(tmp_path, "c1", "met", base="a\n", head="b\n")
    _write_case(tmp_path, "c2", "met", base="a\n", head="b\n")
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(tmp_path)


def test_no_diff_rejected(tmp_path):
    _write_case(tmp_path, "c1", "met", base="same\n", head="same\n")
    _write_case(tmp_path, "c2", "unmet", base="a\n", head="b\n")
    with pytest.raises(AcceptanceSetError):
        read_acceptance_set(tmp_path)


def test_predicted_outcome_only_pass_accepts():
    assert predicted_outcome(Verdict.PASS) == "met"
    assert predicted_outcome(Verdict.BLOCK) == "unmet"
    assert predicted_outcome(Verdict.INDETERMINATE) == "unmet"


@requires_git
def test_materialize_case_builds_a_real_diff(tmp_path):
    _write_case(tmp_path, "c1", "met", base="old\n", head="new\n")
    case = _one(tmp_path / "c1")
    workdir = tmp_path / "repo"
    repo_root, ref = materialize_case(case, workdir)
    log = subprocess.run(
        ["git", "-C", repo_root, "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log == "f.py"
    assert len(ref) == 40


@requires_git
def test_build_review_input_grounds_at_the_change(tmp_path):
    _write_case(tmp_path, "c1", "met", base="old\n", head="new\n")
    case = _one(tmp_path / "c1")
    repo_root, ref = materialize_case(case, tmp_path / "repo")
    review = build_review_input(case, repo_root=repo_root, ref=ref)
    assert review.stage == ReviewStage.ACCEPTANCE_VERIFICATION
    assert review.repo_root == repo_root
    assert review.ref == ref
    assert review.source == "local"
    assert "Acceptance criteria" in review.instructions


@requires_git
def test_snapshot_case_captures_a_repo_diff_into_a_loadable_fixture(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "a@b.invalid")
    git("config", "user.name", "a")
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (repo / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "head")

    dest = tmp_path / "set"
    case_dir = snapshot_case(
        repo_root=repo,
        base_ref="HEAD~1",
        head_ref="HEAD",
        case_id="snap-1",
        acceptance_criteria="f must return 2",
        change_summary="changed f to return 2",
        outcome="met",
        dest_root=dest,
    )
    _write_case(dest, "pad-unmet", "unmet", base="a\n", head="b\n")
    cases = {c.case_id: c for c in read_acceptance_set(dest)}
    assert case_dir == dest / "snap-1"
    assert cases["snap-1"].changed_files() == ["mod.py"]
    assert (case_dir / "base" / "mod.py").read_text(encoding="utf-8").endswith("return 1\n")
    assert (case_dir / "head" / "mod.py").read_text(encoding="utf-8").endswith("return 2\n")


class _VerdictKernel:
    def __init__(self, by_outcome: dict[str, Verdict], cases: list[AcceptanceCase]) -> None:
        self._by_outcome = by_outcome
        self._cases = {c.case_id: c.outcome for c in cases}

    def run(self, review: ReviewInput) -> RawReview:
        return RawReview(verdict=self._by_outcome[self._cases[review.context_ref]], summary="t")


@requires_git
def test_scoring_perfect_agreement(tmp_path):
    _write_case(tmp_path, "a", "met", base="a\n", head="b\n")
    _write_case(tmp_path, "b", "unmet", base="a\n", head="b\n")
    cases = read_acceptance_set(tmp_path)
    kernel = _VerdictKernel({"met": Verdict.PASS, "unmet": Verdict.BLOCK}, cases)
    scorecard = score(cases, kernel)
    assert scorecard.agreements == 2
    assert scorecard.false_accepts == 0
    passed, reasons = assess(scorecard)
    assert passed and reasons == []


@requires_git
def test_scoring_counts_false_accepts(tmp_path):
    _write_case(tmp_path, "a", "met", base="a\n", head="b\n")
    _write_case(tmp_path, "b", "unmet", base="a\n", head="b\n")
    cases = read_acceptance_set(tmp_path)
    kernel = _VerdictKernel({"met": Verdict.PASS, "unmet": Verdict.PASS}, cases)
    scorecard = score(cases, kernel)
    assert scorecard.false_accepts == 1
    passed, reasons = assess(scorecard)
    assert not passed
    assert any("false accept" in r for r in reasons)


@requires_git
def test_scoring_counts_false_blocks(tmp_path):
    _write_case(tmp_path, "a", "met", base="a\n", head="b\n")
    _write_case(tmp_path, "b", "unmet", base="a\n", head="b\n")
    cases = read_acceptance_set(tmp_path)
    kernel = _VerdictKernel({"met": Verdict.BLOCK, "unmet": Verdict.BLOCK}, cases)
    scorecard = score(cases, kernel)
    assert scorecard.false_blocks == 1
    assert scorecard.false_block_rate == 1.0
    assert not assess(scorecard)[0]


def test_adequacy_bar_is_fixed_not_computed():
    assert ADEQUACY_BAR.max_false_accepts == 0
    assert ADEQUACY_BAR.min_agreement_rate == 0.80
    assert ADEQUACY_BAR.max_false_block_rate == 0.20


@requires_git
def test_score_runs_through_the_committed_set_with_an_oracle():
    cases = read_acceptance_set(_ROOT_SET)
    kernel = _VerdictKernel({"met": Verdict.PASS, "unmet": Verdict.BLOCK}, cases)
    scorecard = score(cases, kernel)
    assert scorecard.agreement_rate == 1.0
    assert scorecard.results[0].verdict in ("pass", "block")
