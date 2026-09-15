"""Unit tests for the platform review gate seam and attestation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from services.review.attestation import HmacAttestor, ReviewAttestationError
from services.review.gate import ReviewGate, verdict_payload
from services.review.kernel import (
    FakeReviewKernel,
    RebarReviewKernel,
    _raw_from_decided,
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
    assert record.classification == "standard"
    assert record.content_hash == result.attestation.content_hash
    assert record.metadata["verdict"] == "block"
    assert record.metadata["finding_count"] == 1
    assert record.metadata["summary"] == "1 blocking"
    assert record.metadata["findings"] == [
        {
            "finding_id": "1",
            "severity": "high",
            "blocking": True,
            "title": "bug",
            "detail": "boom",
            "location": None,
        }
    ]


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


def test_raw_from_decided_maps_block_advisory_and_indeterminate():
    decided = [
        {"finding": "real defect", "decision": "block", "reason": "high", "severity": "high"},
        {"finding": "polish", "decision": "advisory", "reason": "default", "severity": "low"},
        {"finding": "unverified", "decision": "indeterminate", "reason": "no-verification"},
        {"finding": "noise", "decision": "dropped", "reason": "veto:cited-reference-inaccurate"},
        {"finding": "", "decision": "advisory"},
    ]

    raw = _raw_from_decided(decided, "review-model", "m")

    assert raw.verdict is Verdict.BLOCK
    assert [f.title for f in raw.findings] == ["real defect", "polish", "unverified"]
    assert [f.blocking for f in raw.findings] == [True, False, False]


def test_raw_from_decided_passes_when_nothing_blocks():
    decided = [{"finding": "polish", "decision": "advisory", "severity": "low"}]

    raw = _raw_from_decided(decided, "review-model", "m")

    assert raw.verdict is Verdict.PASS


@dataclass
class _StubCfg:
    temperature: float = 1.0
    model: str = "review-model"
    repo_path: str | None = None


class _StubRunner:
    def run(self, request):
        if str(request.output_schema or "").endswith("_findings"):
            return {
                "findings": [
                    {
                        "finding": "store.update crashes on a missing id",
                        "criteria": ["correctness"],
                        "evidence": ["app/store.py:20"],
                        "impact": "crash",
                    }
                ]
            }
        return {"verifications": [{"index": 0, "binary": {"cited_reference_accurate": "no"}}]}


def test_run_multipass_drops_a_fabricated_citation_and_passes(monkeypatch):
    from rebar.llm import runner as rebar_runner

    monkeypatch.setattr(rebar_runner, "get_runner", lambda cfg: _StubRunner())
    kernel = RebarReviewKernel()

    for stage in (ReviewStage.PLAN_REVIEW, ReviewStage.CODE_REVIEW, ReviewStage.SECURITY_REVIEW):
        raw = kernel._run_multipass(_input(stage), _StubCfg(), "review-model")
        assert raw.verdict is Verdict.PASS
        assert raw.findings == []


class _UsageStubRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request):
        self.calls += 1
        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "requests": 1,
        }
        if str(request.output_schema or "").endswith("_findings"):
            return {
                "findings": [
                    {
                        "finding": "store.update crashes on a missing id",
                        "criteria": ["correctness"],
                        "evidence": ["app/store.py:20"],
                        "impact": "crash",
                    }
                ],
                "_usage": usage,
            }
        return {
            "verifications": [{"index": 0, "binary": {"cited_reference_accurate": "yes"}}],
            "_usage": usage,
        }


def test_run_multipass_sums_usage_across_finder_and_verifiers(monkeypatch):
    from rebar.llm import runner as rebar_runner

    stub = _UsageStubRunner()
    monkeypatch.setattr(rebar_runner, "get_runner", lambda cfg: stub)
    kernel = RebarReviewKernel()

    raw = kernel._run_multipass(_input(ReviewStage.CODE_REVIEW), _StubCfg(), "review-model")

    assert stub.calls >= 2
    assert raw.usage.prompt_tokens == 10 * stub.calls
    assert raw.usage.completion_tokens == 5 * stub.calls
    assert raw.usage.cache_read_tokens == 2 * stub.calls
    assert raw.usage.cache_write_tokens == 1 * stub.calls
    assert raw.usage.llm_calls == stub.calls


def _keygen(tmp_path):
    import subprocess

    key_path = tmp_path / "signer"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    public_line = (tmp_path / "signer.pub").read_text(encoding="utf-8").strip()
    return str(key_path), public_line


def _signing_settings(tmp_path):
    import yaml
    from scripts.config import Settings

    key_path, public_line = _keygen(tmp_path)
    trust_path = tmp_path / "trust-root.yaml"
    trust_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "principal": "review-signer@nava",
                "keys": [{"id": "k1", "public_key": public_line}],
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        review_signing_enabled=True,
        review_signing_key_path=key_path,
        review_trust_root_path=str(trust_path),
    )


def test_select_attestor_defaults_to_hmac_without_signing():
    from scripts.config import Settings
    from services.review.attestation import HmacAttestor
    from services.review.gate import select_attestor

    attestor = select_attestor(ReviewStage.CODE_REVIEW, Settings(review_signing_enabled=False))
    assert isinstance(attestor, HmacAttestor)


def test_select_attestor_fails_closed_when_signing_enabled_but_unconfigured():
    import pytest
    from scripts.config import Settings
    from services.review.gate import ReviewSigningNotConfigured, select_attestor

    settings = Settings(
        review_signing_enabled=True,
        review_signing_key_path="",
        review_trust_root_path="",
    )
    for stage in ReviewStage:
        with pytest.raises(ReviewSigningNotConfigured):
            select_attestor(stage, settings)


def test_select_attestor_signs_every_stage(tmp_path):
    from services.review.attestation import SshsigAttestor
    from services.review.gate import select_attestor

    settings = _signing_settings(tmp_path)
    for stage in ReviewStage:
        assert isinstance(select_attestor(stage, settings), SshsigAttestor)


def test_select_attestor_uses_hmac_when_signing_disabled(tmp_path):
    from scripts.config import Settings
    from services.review.attestation import HmacAttestor
    from services.review.gate import select_attestor

    settings = Settings(review_signing_enabled=False)
    for stage in ReviewStage:
        assert isinstance(select_attestor(stage, settings), HmacAttestor)


async def test_gate_records_the_envelope_field_in_evidence():
    sink = _RecordingSink()
    gate = ReviewGate(FakeReviewKernel(_clean_raw()), sink, HmacAttestor("s3cret"))

    await gate.review(_input(), execution_id="exec-env")

    assert "envelope" in sink.records[0].metadata
    assert sink.records[0].metadata["envelope"] == ""


async def test_signed_gate_verifies_and_records_scheme(tmp_path):
    from services.review.gate import select_attestor

    settings = _signing_settings(tmp_path)
    attestor = select_attestor(ReviewStage.CODE_REVIEW, settings)
    sink = _RecordingSink()
    gate = ReviewGate(FakeReviewKernel(_clean_raw()), sink, attestor)

    result = await gate.review(_input(ReviewStage.CODE_REVIEW), execution_id="exec-signed")

    assert result.attestation.algorithm == "sshsig"
    assert result.attestation.key_id == "review-signer@nava"
    record = sink.records[0]
    assert record.metadata["algorithm"] == "sshsig"
    assert record.metadata["key_id"] == "review-signer@nava"
    assert record.metadata["envelope"]
    payload = verdict_payload(_input(ReviewStage.CODE_REVIEW), _clean_raw())
    assert attestor.verify(result.attestation, payload) is True


