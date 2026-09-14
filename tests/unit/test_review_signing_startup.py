"""Tests for the review-worker signing preflight and trust-root change auditing."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from scripts.config import Settings
from services.orchestration.review_signing import (
    ReviewSigningConfigError,
    plan_trust_root_audit,
    review_signing_preflight,
)
from services.review.trust_root import TrustRoot, TrustRootKey

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample review-signer@nava"


def _trust_root(*key_ids: str, revoked: tuple[str, ...] = ()) -> TrustRoot:
    keys = tuple(
        TrustRootKey(key_id=key_id, public_key=_pub, revoked=key_id in revoked)
        for key_id in key_ids
    )
    return TrustRoot(version=1, principal="review-signer@nava", keys=keys)


def test_plan_trust_root_audit_initialized():
    event = plan_trust_root_audit(None, _trust_root("k1"))
    assert event is not None
    assert event["change"] == "initialized"
    assert event["fingerprint"]
    assert event["snapshot"]["principal"] == "review-signer@nava"


def test_plan_trust_root_audit_unchanged_returns_none():
    root = _trust_root("k1")
    same = _trust_root("k1")
    assert plan_trust_root_audit(root, same) is None


def test_plan_trust_root_audit_added_key():
    event = plan_trust_root_audit(_trust_root("k1"), _trust_root("k1", "k2"))
    assert event is not None
    assert "added 1" in event["change"]
    assert event["key_ids"] == ["k2"]


def test_plan_trust_root_audit_revoked_key():
    event = plan_trust_root_audit(
        _trust_root("k1", "k2"), _trust_root("k1", "k2", revoked=("k2",))
    )
    assert event is not None
    assert "revoked 1" in event["change"]
    assert event["key_ids"] == ["k2"]


def test_preflight_noop_when_signing_disabled():
    review_signing_preflight(Settings(review_signing_enabled=False))


def test_preflight_fails_when_paths_missing():
    settings = Settings(
        review_signing_enabled=True,
        review_signing_key_path="",
        review_trust_root_path="",
    )
    with pytest.raises(ReviewSigningConfigError):
        review_signing_preflight(settings)


def test_preflight_fails_when_key_absent(tmp_path):
    import yaml

    trust_path = tmp_path / "trust-root.yaml"
    trust_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "principal": "review-signer@nava",
                "keys": [{"id": "k1", "public_key": _pub}],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        review_signing_enabled=True,
        review_signing_key_path=str(tmp_path / "missing-key"),
        review_trust_root_path=str(trust_path),
    )
    with pytest.raises(ReviewSigningConfigError):
        review_signing_preflight(settings)


@pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")
def test_preflight_passes_with_valid_config(tmp_path):
    import yaml

    key_path = tmp_path / "signer"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    public_line = (tmp_path / "signer.pub").read_text(encoding="utf-8").strip()
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
    settings = Settings(
        review_signing_enabled=True,
        review_signing_key_path=str(key_path),
        review_trust_root_path=str(trust_path),
    )
    review_signing_preflight(settings)
