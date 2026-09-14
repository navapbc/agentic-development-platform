"""Unit tests for the review verdict attestors (HMAC and SSHSIG-over-DSSE)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rebar.attest.authorship import allowed_signers_from_keys
from rebar.attest.sshsig import ssh_keygen_version
from services.review.attestation import (
    HmacAttestor,
    ReviewAttestationError,
    SshsigAttestor,
)

_PRINCIPAL = "review-signer@nava"
requires_ssh_keygen = pytest.mark.skipif(
    ssh_keygen_version() is None, reason="ssh-keygen (OpenSSH >= 8.9) not available"
)


def _payload(verdict: str = "pass") -> dict:
    return {"stage": "code-review", "verdict": verdict, "findings": []}


def _keygen(tmp_path: Path, name: str = "id_ed25519") -> tuple[str, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    public_line = (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()
    return str(key_path), public_line


def test_hmac_attestor_signs_and_verifies():
    attestor = HmacAttestor("secret")
    payload = _payload()
    attestation = attestor.sign(payload)
    assert attestor.verify(attestation, payload) is True


def test_hmac_attestor_rejects_a_tampered_payload():
    attestor = HmacAttestor("secret")
    attestation = attestor.sign(_payload("pass"))
    assert attestor.verify(attestation, _payload("block")) is False


@requires_ssh_keygen
def test_sshsig_attestor_signs_and_verifies_against_trust_root(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path)
    trust_root = allowed_signers_from_keys([public_line], principal=_PRINCIPAL)
    attestor = SshsigAttestor(key_path=key_path, principal=_PRINCIPAL, trust_root=trust_root)

    payload = _payload()
    attestation = attestor.sign(payload)

    assert attestation.algorithm == "sshsig"
    assert attestation.key_id == _PRINCIPAL
    assert attestation.envelope
    assert attestor.verify(attestation, payload) is True


@requires_ssh_keygen
def test_sshsig_attestor_rejects_a_tampered_payload(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path)
    trust_root = allowed_signers_from_keys([public_line], principal=_PRINCIPAL)
    attestor = SshsigAttestor(key_path=key_path, principal=_PRINCIPAL, trust_root=trust_root)

    attestation = attestor.sign(_payload("pass"))
    assert attestor.verify(attestation, _payload("block")) is False


@requires_ssh_keygen
def test_sshsig_attestor_rejects_a_key_outside_the_trust_root(tmp_path: Path):
    signer_key, _ = _keygen(tmp_path, "signer")
    _, other_public = _keygen(tmp_path, "other")
    trust_root = allowed_signers_from_keys([other_public], principal=_PRINCIPAL)
    attestor = SshsigAttestor(key_path=signer_key, principal=_PRINCIPAL, trust_root=trust_root)

    payload = _payload()
    attestation = attestor.sign(payload)
    assert attestor.verify(attestation, payload) is False


@requires_ssh_keygen
def test_sshsig_attestor_rejects_an_absent_or_malformed_envelope(tmp_path: Path):
    key_path, public_line = _keygen(tmp_path)
    trust_root = allowed_signers_from_keys([public_line], principal=_PRINCIPAL)
    attestor = SshsigAttestor(key_path=key_path, principal=_PRINCIPAL, trust_root=trust_root)

    payload = _payload()
    signed = attestor.sign(payload)

    from dataclasses import replace

    assert attestor.verify(replace(signed, envelope=""), payload) is False
    assert attestor.verify(replace(signed, envelope="{not json"), payload) is False


def test_sshsig_attestor_construction_fails_without_ssh_keygen(monkeypatch, tmp_path: Path):
    import rebar.attest.sshsig as sshsig
    from rebar.attest.sshsig import SshKeygenUnavailable

    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    with pytest.raises(SshKeygenUnavailable):
        SshsigAttestor(key_path=str(tmp_path / "k"), principal=_PRINCIPAL, trust_root="")


def test_review_attestation_error_is_exported():
    assert issubclass(ReviewAttestationError, Exception)
