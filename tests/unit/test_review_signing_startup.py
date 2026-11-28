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
_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample review-signer@adp"


def _trust_root(*key_ids: str, revoked: tuple[str, ...] = ()) -> TrustRoot:
    keys = tuple(
        TrustRootKey(key_id=key_id, public_key=_pub, tenant="adp", revoked=key_id in revoked)
        for key_id in key_ids
    )
    return TrustRoot(version=1, keys=keys)


def _keygen(tmp_path, name: str) -> tuple[str, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    return str(key_path), (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()


def _write_trust_root(tmp_path, keys: list[dict]):
    import yaml

    trust_path = tmp_path / "trust-root.yaml"
    trust_path.write_text(yaml.safe_dump({"version": 1, "keys": keys}), encoding="utf-8")
    return trust_path


def _signing_settings(key_path: str, trust_path, tenant: str) -> Settings:
    return Settings(
        review_signing_enabled=True,
        review_signing_key_path=key_path,
        review_trust_root_path=str(trust_path),
        tenant=tenant,
    )


def test_plan_trust_root_audit_initialized():
    event = plan_trust_root_audit(None, _trust_root("k1"))
    assert event is not None
    assert event["change"] == "initialized"
    assert event["fingerprint"]
    assert event["tenants"] == ["adp"]
    assert event["snapshot"]["keys"][0]["tenant"] == "adp"


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
    event = plan_trust_root_audit(_trust_root("k1", "k2"), _trust_root("k1", "k2", revoked=("k2",)))
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
    trust_path = _write_trust_root(tmp_path, [{"id": "k1", "tenant": "adp", "public_key": _pub}])
    settings = _signing_settings(str(tmp_path / "missing-key"), trust_path, "adp")
    with pytest.raises(ReviewSigningConfigError):
        review_signing_preflight(settings)


@pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")
def test_preflight_passes_with_valid_config(tmp_path):
    key_path, public_line = _keygen(tmp_path, "signer")
    trust_path = _write_trust_root(
        tmp_path, [{"id": "k1", "tenant": "adp", "public_key": public_line}]
    )
    review_signing_preflight(_signing_settings(key_path, trust_path, "adp"))


@pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")
def test_preflight_refuses_a_worker_holding_another_tenants_key(tmp_path):
    adp_key, adp_public = _keygen(tmp_path, "adp")
    _, beta_public = _keygen(tmp_path, "beta")
    trust_path = _write_trust_root(
        tmp_path,
        [
            {"id": "adp-1", "tenant": "adp", "public_key": adp_public},
            {"id": "beta-1", "tenant": "beta", "public_key": beta_public},
        ],
    )

    with pytest.raises(ReviewSigningConfigError, match="'beta'"):
        review_signing_preflight(_signing_settings(adp_key, trust_path, "beta"))


@pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")
def test_preflight_refuses_a_worker_without_tenant(tmp_path):
    key_path, public_line = _keygen(tmp_path, "signer")
    trust_path = _write_trust_root(
        tmp_path, [{"id": "k1", "tenant": "adp", "public_key": public_line}]
    )

    with pytest.raises(ReviewSigningConfigError, match="TENANT is not set"):
        review_signing_preflight(_signing_settings(key_path, trust_path, ""))
