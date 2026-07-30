"""Hermetic tests for the code-review and security-review gate activities."""

from __future__ import annotations

from services.orchestration.temporal_activities import review
from services.orchestration.temporal_activities.review import (
    ReviewRequest,
    review_code,
    review_security,
    verify_acceptance,
)
from services.review.attestation import HmacAttestor
from services.review.gate import ReviewGate
from services.review.types import (
    STAGE_TIERS,
    Finding,
    ModelProvenance,
    RawReview,
    ReviewStage,
    Verdict,
)


class _CapturingKernel:
    def __init__(self, raw: RawReview) -> None:
        self._raw = raw
        self.seen: list = []

    def run(self, review_input) -> RawReview:
        self.seen.append(review_input)
        return self._raw


class _Sink:
    def __init__(self) -> None:
        self.records: list = []

    async def persist(self, record) -> str:
        self.records.append(record)
        return record.artifact_id


def _provenance(tier: str) -> list[ModelProvenance]:
    return [ModelProvenance(leg="finder", tier=tier, model_id=f"openai:{tier}")]


def _clean(tier: str) -> RawReview:
    return RawReview(verdict=Verdict.PASS, provenance=_provenance(tier), summary="clean")


def _blocking(tier: str) -> RawReview:
    return RawReview(
        verdict=Verdict.BLOCK,
        findings=[
            Finding(finding_id="1", severity="high", blocking=True, title="SQLi", detail="x")
        ],
        provenance=_provenance(tier),
        summary="1 blocking",
    )


async def _invoke(monkeypatch, activity_fn, raw: RawReview):
    kernel = _CapturingKernel(raw)
    sink = _Sink()
    monkeypatch.setattr(
        review, "build_review_gate", lambda: ReviewGate(kernel, sink, HmacAttestor("t"))
    )
    req = ReviewRequest(
        task_id="T-1", execution_id="exec-1", subject="a diff", context_summary="ctx"
    )
    outcome = await activity_fn(req)
    return outcome, kernel, sink


async def test_review_code_clean(monkeypatch):
    outcome, kernel, sink = await _invoke(monkeypatch, review_code, _clean("review-model"))

    assert outcome.verdict == "pass"
    assert outcome.blocking_findings == 0
    assert outcome.findings == []
    assert outcome.prompt_version == "code-review/v1"
    assert kernel.seen[0].stage is ReviewStage.CODE_REVIEW
    assert STAGE_TIERS[kernel.seen[0].stage] == "review-model"
    assert len(sink.records) == 1
    assert sink.records[0].content_hash == outcome.attestation_hash


async def test_review_code_blocking(monkeypatch):
    outcome, _, sink = await _invoke(monkeypatch, review_code, _blocking("review-model"))

    assert outcome.verdict == "block"
    assert outcome.blocking_findings == 1
    assert outcome.findings == ["SQLi"]
    assert len(sink.records) == 1


async def test_review_security_clean(monkeypatch):
    outcome, kernel, _ = await _invoke(
        monkeypatch, review_security, _clean("security-review-model")
    )

    assert outcome.verdict == "pass"
    assert outcome.prompt_version == "security-review/v1"
    assert kernel.seen[0].stage is ReviewStage.SECURITY_REVIEW
    assert STAGE_TIERS[kernel.seen[0].stage] == "security-review-model"


async def test_review_security_blocking(monkeypatch):
    outcome, _, sink = await _invoke(
        monkeypatch, review_security, _blocking("security-review-model")
    )

    assert outcome.verdict == "block"
    assert outcome.findings == ["SQLi"]
    assert len(sink.records) == 1


async def test_verify_acceptance_clean(monkeypatch):
    outcome, kernel, _ = await _invoke(monkeypatch, verify_acceptance, _clean("evaluation-model"))

    assert outcome.verdict == "pass"
    assert outcome.prompt_version == "acceptance-verification/v1"
    assert kernel.seen[0].stage is ReviewStage.ACCEPTANCE_VERIFICATION
    assert STAGE_TIERS[kernel.seen[0].stage] == "evaluation-model"


async def test_verify_acceptance_blocking(monkeypatch):
    outcome, _, sink = await _invoke(monkeypatch, verify_acceptance, _blocking("evaluation-model"))

    assert outcome.verdict == "block"
    assert outcome.findings == ["SQLi"]
    assert len(sink.records) == 1
