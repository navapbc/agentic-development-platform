"""Score the acceptance gate over a labeled set and record a scorecard.

The harness runs the acceptance gate through the production kernel path over each
labeled case and scores the verdict against the known outcome. The adequacy bar is
fixed below and read rather than computed from results, so the assessment measures
against a bar set before the run.

Run the live scored run with:

    python -m services.review.eval.acceptance_harness

It writes a scorecard under ``services/review/eval/scorecards/``. The live run
consumes model calls through the configured review path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.review.eval.acceptance_schema import (
    OUTCOME_MET,
    OUTCOME_UNMET,
    AcceptanceCase,
    read_acceptance_set,
)
from services.review.types import ReviewInput, ReviewStage, Verdict

HARNESS_VERSION = "acceptance-harness/1"
PROMPT_NAME = "acceptance-verification"
PROMPT_VERSION_TAG = "v1"

_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_SET = _ROOT / "services/review/eval/acceptance_set.jsonl"
_SCORECARD_DIR = _ROOT / "services/review/eval/scorecards"


@dataclass(frozen=True)
class AdequacyBar:
    min_agreement_rate: float
    max_false_accepts: int
    max_false_block_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_agreement_rate": self.min_agreement_rate,
            "max_false_accepts": self.max_false_accepts,
            "max_false_block_rate": self.max_false_block_rate,
        }


ADEQUACY_BAR = AdequacyBar(
    min_agreement_rate=0.80,
    max_false_accepts=0,
    max_false_block_rate=0.20,
)


def predicted_outcome(verdict: Verdict) -> str:
    """Map a verdict to a predicted outcome. Only a pass accepts the completion."""
    return OUTCOME_MET if verdict == Verdict.PASS else OUTCOME_UNMET


def build_review_input(case: AcceptanceCase) -> ReviewInput:
    subject = (
        f"Acceptance criteria:\n{case.acceptance_criteria}\n\n"
        f"Completion evidence:\n{case.completion_evidence}"
    )
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    return ReviewInput(
        stage=ReviewStage.ACCEPTANCE_VERIFICATION,
        subject_ref=f"sha256:{digest}",
        context_ref=case.case_id,
        prompt_name=PROMPT_NAME,
        prompt_version=PROMPT_VERSION_TAG,
        system_prompt="You verify whether a change meets its acceptance criteria.",
        instructions=subject,
        source="acceptance-eval",
    )


@dataclass
class CaseResult:
    case_id: str
    outcome: str
    predicted: str
    verdict: str

    @property
    def agreement(self) -> bool:
        return self.outcome == self.predicted

    @property
    def false_accept(self) -> bool:
        return self.outcome == OUTCOME_UNMET and self.predicted == OUTCOME_MET

    @property
    def false_block(self) -> bool:
        return self.outcome == OUTCOME_MET and self.predicted == OUTCOME_UNMET

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "outcome": self.outcome,
            "predicted": self.predicted,
            "verdict": self.verdict,
            "agreement": self.agreement,
            "false_accept": self.false_accept,
            "false_block": self.false_block,
        }


@dataclass
class Scorecard:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def agreements(self) -> int:
        return sum(1 for r in self.results if r.agreement)

    @property
    def false_accepts(self) -> int:
        return sum(1 for r in self.results if r.false_accept)

    @property
    def false_blocks(self) -> int:
        return sum(1 for r in self.results if r.false_block)

    @property
    def met_cases(self) -> int:
        return sum(1 for r in self.results if r.outcome == OUTCOME_MET)

    @property
    def unmet_cases(self) -> int:
        return sum(1 for r in self.results if r.outcome == OUTCOME_UNMET)

    @property
    def agreement_rate(self) -> float:
        return self.agreements / self.total if self.total else 0.0

    @property
    def false_block_rate(self) -> float:
        return self.false_blocks / self.met_cases if self.met_cases else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "acceptance_scorecard",
            "harness_version": HARNESS_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "totals": {
                "cases": self.total,
                "met_cases": self.met_cases,
                "unmet_cases": self.unmet_cases,
                "agreements": self.agreements,
                "false_accepts": self.false_accepts,
                "false_blocks": self.false_blocks,
            },
            "rates": {
                "agreement_rate": round(self.agreement_rate, 4),
                "false_block_rate": round(self.false_block_rate, 4),
            },
            "adequacy_bar": ADEQUACY_BAR.to_dict(),
            "cases": [r.to_dict() for r in self.results],
        }


def score(cases: list[AcceptanceCase], kernel: Any) -> Scorecard:
    """Run the acceptance gate over each case and score verdicts against the labels."""
    scorecard = Scorecard()
    for case in cases:
        raw = kernel.run(build_review_input(case))
        predicted = predicted_outcome(raw.verdict)
        scorecard.results.append(
            CaseResult(
                case_id=case.case_id,
                outcome=case.outcome,
                predicted=predicted,
                verdict=raw.verdict.value,
            )
        )
    return scorecard


def assess(scorecard: Scorecard, bar: AdequacyBar = ADEQUACY_BAR) -> tuple[bool, list[str]]:
    """Compare a scorecard to the fixed adequacy bar and return the pass flag and reasons."""
    reasons: list[str] = []
    if scorecard.agreement_rate < bar.min_agreement_rate:
        reasons.append(
            f"agreement rate {scorecard.agreement_rate:.2f} below {bar.min_agreement_rate:.2f}"
        )
    if scorecard.false_accepts > bar.max_false_accepts:
        reasons.append(
            f"{scorecard.false_accepts} false accepts exceed {bar.max_false_accepts}"
        )
    if scorecard.false_block_rate > bar.max_false_block_rate:
        rate = scorecard.false_block_rate
        reasons.append(f"false block rate {rate:.2f} above {bar.max_false_block_rate:.2f}")
    return (not reasons), reasons


def _write_scorecard(scorecard: Scorecard) -> Path:
    _SCORECARD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    target = _SCORECARD_DIR / f"acceptance.{stamp}.json"
    target.write_text(json.dumps(scorecard.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the live acceptance-gate scored run.")
    parser.add_argument("--set", default=str(_DEFAULT_SET), help="Path to the labeled set.")
    args = parser.parse_args(argv)

    from services.review.kernel import RebarReviewKernel

    cases = read_acceptance_set(args.set)
    scorecard = score(cases, RebarReviewKernel())
    passed, reasons = assess(scorecard)
    target = _write_scorecard(scorecard)
    print(json.dumps(scorecard.to_dict()["rates"], sort_keys=True))
    print(f"[acceptance-eval] scorecard written to {target}")
    print(f"[acceptance-eval] {'PASS' if passed else 'FAIL'}")
    for reason in reasons:
        print(f"[acceptance-eval] {reason}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
