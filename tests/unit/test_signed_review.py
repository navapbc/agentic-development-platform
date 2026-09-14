"""End-to-end fail-closed proof for the signed review path (U5)."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from rebar.attest.authorship import allowed_signers_from_keys
from rebar.attest.sshsig import ssh_keygen_version
from services.review.attestation import ReviewAttestationError, SshsigAttestor
from services.review.gate import ReviewGate, verdict_payload
from services.review.kernel import FakeReviewKernel
from services.review.trust_root import parse_trust_root
from services.review.types import ModelProvenance, RawReview, ReviewInput, ReviewStage, Verdict

_PRINCIPAL = "review-signer@nava"
requires_ssh_keygen = pytest.mark.skipif(
    ssh_keygen_version() is None, reason="ssh-keygen (OpenSSH >= 8.9) not available"
)
pytestmark = requires_ssh_keygen


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list = []

    async def persist(self, record) -> str:
        self.records.append(record)
        return record.artifact_id


def _keygen(tmp_path: Path, name: str) -> tuple[str, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    return str(key_path), (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()


def _input() -> ReviewInput:
    return ReviewInput(
        stage=ReviewStage.CODE_REVIEW,
        subject_ref="sha256:abc",
        context_ref="ctx-1",
        prompt_name="code-review",
        prompt_version="v1",
        system_prompt="You are a reviewer.",
        instructions="Review the change.",
        reviewers=("reviewer-a",),
    )


def _raw() -> RawReview:
    return RawReview(
        verdict=Verdict.PASS,
        provenance=[ModelProvenance(leg="finder", tier="review-model", model_id="m")],
        summary="clean",
    )


def _attestor(key_path: str, trust_root: str) -> SshsigAttestor:
    return SshsigAttestor(key_path=key_path, principal=_PRINCIPAL, trust_root=trust_root)


async def test_valid_signed_verdict_is_accepted_and_reverifies_offline(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path, "signer")
    trust_root = allowed_signers_from_keys([public_line], principal=_PRINCIPAL)
    attestor = _attestor(key_path, trust_root)
    sink = _RecordingSink()
    gate = ReviewGate(FakeReviewKernel(_raw()), sink, attestor)

    result = await gate.review(_input(), execution_id="exec-1")

    assert result.attestation.algorithm == "sshsig"
    assert len(sink.records) == 1
    envelope = sink.records[0].metadata["envelope"]
    assert envelope
    offline = _attestor(key_path, trust_root)
    stored = replace(result.attestation, envelope=envelope)
    assert offline.verify(stored, verdict_payload(_input(), _raw())) is True


async def test_out_of_trust_root_signer_blocks(tmp_path: Path):
    signer_key, _ = _keygen(tmp_path, "signer")
    _, other_public = _keygen(tmp_path, "other")
    trust_root = allowed_signers_from_keys([other_public], principal=_PRINCIPAL)
    gate = ReviewGate(FakeReviewKernel(_raw()), _RecordingSink(), _attestor(signer_key, trust_root))

    with pytest.raises(ReviewAttestationError):
        await gate.review(_input(), execution_id="exec-2")


async def test_revoked_key_blocks(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path, "signer")
    trust_root = parse_trust_root(
        {
            "version": 1,
            "principal": _PRINCIPAL,
            "keys": [{"id": "k1", "public_key": public_line, "revoked": True}],
        }
    )
    sink = _RecordingSink()
    gate = ReviewGate(
        FakeReviewKernel(_raw()), sink, _attestor(key_path, trust_root.allowed_signers())
    )

    with pytest.raises(ReviewAttestationError):
        await gate.review(_input(), execution_id="exec-3")
    assert sink.records == []


async def test_tampered_payload_fails_offline_reverification(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path, "signer")
    trust_root = allowed_signers_from_keys([public_line], principal=_PRINCIPAL)
    attestor = _attestor(key_path, trust_root)

    attestation = attestor.sign(verdict_payload(_input(), _raw()))
    blocked = RawReview(
        verdict=Verdict.BLOCK,
        provenance=[ModelProvenance(leg="finder", tier="review-model", model_id="m")],
        summary="tampered",
    )
    assert attestor.verify(attestation, verdict_payload(_input(), blocked)) is False


async def test_absent_envelope_fails_verification(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path, "signer")
    trust_root = allowed_signers_from_keys([public_line], principal=_PRINCIPAL)
    attestor = _attestor(key_path, trust_root)

    attestation = attestor.sign(verdict_payload(_input(), _raw()))
    cleared = replace(attestation, envelope="")
    assert attestor.verify(cleared, verdict_payload(_input(), _raw())) is False
