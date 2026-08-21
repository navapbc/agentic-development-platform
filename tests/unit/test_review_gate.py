"""Unit tests for the platform review gate seam and attestation."""

from __future__ import annotations

import pytest
from services.review.attestation import HmacAttestor, ReviewAttestationError
from services.review.gate import ReviewGate, verdict_payload
from services.review.kernel import (
    DEFAULT_BLOCKING_SEVERITIES,
    FakeReviewKernel,
    _raw_from_runner_result,
)
from services.review.types import (
    Finding,
    ModelProvenance,
    RawReview,
    ReviewInput,
    ReviewStage,
    Verdict,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list = []

    async def persist(self, record) -> str:
        self.records.append(record)
        return record.artifact_id


class _RejectingAttestor:
    def __init__(self, inner: HmacAttestor) -> None:
        self._inner = inner

    def sign(self, payload):
        return self._inner.sign(payload)

    def verify(self, attestation, payload) -> bool:
        return False


def _input(stage: ReviewStage = ReviewStage.PLAN_REVIEW) -> ReviewInput:
    return ReviewInput(
        stage=stage,
        subject_ref="sha256:abc",
        context_ref="ctx-1",
        prompt_name="plan-review",
        prompt_version="v1",
        system_prompt="You are a reviewer.",
        instructions="Review the plan.",
        reviewers=("reviewer-a",),
    )


def _finder(model: str = "openai:review-model") -> list[ModelProvenance]:
    return [ModelProvenance(leg="finder", tier="review-model", model_id=model)]


def _clean_raw() -> RawReview:
    return RawReview(verdict=Verdict.PASS, provenance=_finder(), summary="clean")


def _blocking_raw() -> RawReview:
    return RawReview(
        verdict=Verdict.BLOCK,
        findings=[
            Finding(finding_id="1", severity="high", blocking=True, title="bug", detail="boom")
        ],
        coaching=["fix it"],
        provenance=_finder(),
        summary="1 blocking",
    )


async def test_gate_returns_normalized_verdict_and_persists_evidence():
    sink = _RecordingSink()
    gate = ReviewGate(FakeReviewKernel(_blocking_raw()), sink, HmacAttestor("s3cret"))

    result = await gate.review(_input(), execution_id="exec-1")

    assert result.verdict is Verdict.BLOCK
    assert [f.title for f in result.findings] == ["bug"]
    assert result.coaching == ["fix it"]
    assert result.attestation.algorithm == "HMAC-SHA256"
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.execution_id == "exec-1"
    assert record.source_component == "review-kernel"
    assert record.classification == "final"
    assert record.content_hash == result.attestation.content_hash
    assert record.metadata["verdict"] == "block"


async def test_gate_attestation_verifies_and_detects_tampering():
    raw = _clean_raw()
    review = _input()
    attestor = HmacAttestor("s3cret")
    gate = ReviewGate(FakeReviewKernel(raw), _RecordingSink(), attestor)

    result = await gate.review(review, execution_id="exec-2")

    payload = verdict_payload(review, raw)
    assert attestor.verify(result.attestation, payload) is True
    tampered = dict(payload)
    tampered["verdict"] = "pass-tampered"
    assert attestor.verify(result.attestation, tampered) is False


async def test_gate_fails_closed_when_attestation_does_not_verify():
    sink = _RecordingSink()
    gate = ReviewGate(
        FakeReviewKernel(_clean_raw()), sink, _RejectingAttestor(HmacAttestor("s3cret"))
    )

    with pytest.raises(ReviewAttestationError):
        await gate.review(_input(), execution_id="exec-3")

    assert sink.records == []


def test_runner_result_maps_to_blocking_verdict():
    raw = _raw_from_runner_result(
        {
            "model": "openai:review-model",
            "summary": "1 finding",
            "findings": [{"severity": "high", "blocking": True, "title": "SQLi", "detail": "bad"}],
        },
        "review-model",
        DEFAULT_BLOCKING_SEVERITIES,
    )

    assert raw.verdict is Verdict.BLOCK
    assert raw.findings[0].title == "SQLi"
    assert raw.provenance[0].model_id == "openai:review-model"


def test_runner_result_maps_clean_to_pass():
    raw = _raw_from_runner_result(
        {"model": "m", "summary": "clean", "findings": []},
        "review-model",
        DEFAULT_BLOCKING_SEVERITIES,
    )

    assert raw.verdict is Verdict.PASS
    assert raw.findings == []


def test_severity_drives_blocking_without_an_explicit_flag():
    raw = _raw_from_runner_result(
        {
            "model": "m",
            "findings": [{"severity": "high", "title": "TLS disabled", "detail": "mitm"}],
        },
        "security-review-model",
        DEFAULT_BLOCKING_SEVERITIES,
    )

    assert raw.verdict is Verdict.BLOCK
    assert raw.findings[0].blocking is True


def test_below_threshold_severity_is_advisory():
    raw = _raw_from_runner_result(
        {"model": "m", "findings": [{"severity": "low", "title": "style nit", "detail": "x"}]},
        "review-model",
        DEFAULT_BLOCKING_SEVERITIES,
    )

    assert raw.verdict is Verdict.PASS
    assert raw.findings[0].blocking is False


def test_explicit_blocking_flag_overrides_the_severity_threshold():
    raw = _raw_from_runner_result(
        {"model": "m", "findings": [{"severity": "low", "blocking": True, "title": "must fix"}]},
        "review-model",
        DEFAULT_BLOCKING_SEVERITIES,
    )

    assert raw.verdict is Verdict.BLOCK
    assert raw.findings[0].blocking is True


def test_configured_threshold_narrows_blocking_to_high_and_critical():
    raw = _raw_from_runner_result(
        {"model": "m", "findings": [{"severity": "medium", "title": "maybe"}]},
        "review-model",
        frozenset({"high", "critical"}),
    )

    assert raw.verdict is Verdict.PASS
    assert raw.findings[0].blocking is False
