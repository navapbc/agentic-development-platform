"""Unit tests for the review trust-root loader (revocation and rotation)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from services.review.trust_root import (
    TrustRootError,
    load_trust_root,
    parse_trust_root,
)

_TENANT = "adp"
_PRINCIPAL = "review-signer@adp"


def _public_key(tmp_path: Path, name: str) -> str:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    return (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()


def _bundle(keys: list[dict]) -> dict:
    return {"version": 1, "keys": [{"tenant": _TENANT, **key} for key in keys]}


def test_active_key_renders_into_allowed_signers():
    tr = parse_trust_root(_bundle([{"id": "k1", "public_key": "ssh-ed25519 AAAAkey1"}]))
    signers = tr.allowed_signers(_TENANT)
    assert signers == f"{_PRINCIPAL} ssh-ed25519 AAAAkey1"
    assert tr.active_key_ids(_TENANT) == ["k1"]


def test_revoked_key_is_excluded():
    tr = parse_trust_root(
        _bundle(
            [
                {"id": "k1", "public_key": "ssh-ed25519 AAAAkey1", "revoked": True},
                {"id": "k2", "public_key": "ssh-ed25519 AAAAkey2"},
            ]
        )
    )
    assert tr.active_key_ids(_TENANT) == ["k2"]
    assert "AAAAkey1" not in tr.allowed_signers(_TENANT)


def test_out_of_window_key_is_excluded():
    tr = parse_trust_root(
        _bundle(
            [
                {
                    "id": "expired",
                    "public_key": "ssh-ed25519 AAAAexp",
                    "not_after": "2020-01-01T00:00:00Z",
                },
                {
                    "id": "future",
                    "public_key": "ssh-ed25519 AAAAfut",
                    "not_before": "2999-01-01T00:00:00Z",
                },
            ]
        )
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert tr.active_key_ids(_TENANT, now) == []
    assert tr.allowed_signers(_TENANT, now) == ""


def test_rotation_window_admits_both_then_only_new():
    tr = parse_trust_root(
        _bundle(
            [
                {
                    "id": "old",
                    "public_key": "ssh-ed25519 AAAAold",
                    "not_after": "2026-06-15T00:00:00Z",
                },
                {
                    "id": "new",
                    "public_key": "ssh-ed25519 AAAAnew",
                    "not_before": "2026-06-01T00:00:00Z",
                },
            ]
        )
    )
    during = datetime(2026, 6, 10, tzinfo=UTC)
    assert sorted(tr.active_key_ids(_TENANT, during)) == ["new", "old"]

    after = datetime(2026, 7, 1, tzinfo=UTC)
    assert tr.active_key_ids(_TENANT, after) == ["new"]


def test_malformed_bundle_fails_closed():
    with pytest.raises(TrustRootError):
        parse_trust_root({"keys": []})
    with pytest.raises(TrustRootError):
        parse_trust_root(_bundle([]))
    with pytest.raises(TrustRootError):
        parse_trust_root(_bundle([{"id": "k1"}]))
    with pytest.raises(TrustRootError):
        parse_trust_root("not a mapping")


def test_duplicate_key_ids_rejected():
    with pytest.raises(TrustRootError):
        parse_trust_root(
            _bundle(
                [
                    {"id": "dup", "public_key": "ssh-ed25519 A"},
                    {"id": "dup", "public_key": "ssh-ed25519 B"},
                ]
            )
        )


def test_load_trust_root_reads_yaml_file(tmp_path: Path):
    public_line = _public_key(tmp_path, "signer")
    bundle_path = tmp_path / "trust-root.yaml"
    bundle_path.write_text(
        yaml.safe_dump(_bundle([{"id": "k1", "public_key": public_line}])),
        encoding="utf-8",
    )
    tr = load_trust_root(bundle_path)
    assert tr.tenants() == [_TENANT]
    assert public_line in tr.allowed_signers(_TENANT)


def test_key_without_tenant_is_refused_with_its_id():
    with pytest.raises(TrustRootError, match="'untenanted'"):
        parse_trust_root(
            {"version": 1, "keys": [{"id": "untenanted", "public_key": "ssh-ed25519 A"}]}
        )


def test_only_the_named_tenants_keys_render_under_its_principal():
    tr = parse_trust_root(
        {
            "version": 1,
            "keys": [
                {"id": "adp-1", "tenant": "adp", "public_key": "ssh-ed25519 AAAAadp"},
                {"id": "beta-1", "tenant": "beta", "public_key": "ssh-ed25519 AAAAbeta"},
            ],
        }
    )

    assert tr.allowed_signers("beta") == "review-signer@beta ssh-ed25519 AAAAbeta"
    assert tr.active_key_ids("adp") == ["adp-1"]
    assert tr.active_keys("") == ()


def test_load_trust_root_missing_file_fails_closed(tmp_path: Path):
    with pytest.raises(TrustRootError):
        load_trust_root(tmp_path / "nope.yaml")


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(event)


def test_audit_trust_root_change_records_actor_change_and_keys():
    from services.review.trust_root import audit_trust_root_change

    old = parse_trust_root(_bundle([{"id": "k1", "public_key": "ssh-ed25519 A"}]))
    new = parse_trust_root(
        _bundle(
            [
                {"id": "k1", "public_key": "ssh-ed25519 A", "revoked": True},
                {"id": "k2", "public_key": "ssh-ed25519 B"},
            ]
        )
    )
    sink = _RecordingAudit()

    audit_trust_root_change(sink, actor="ops@nava", old=old, new=new)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["event"] == "review_trust_root_change"
    assert event["actor"] == "ops@nava"
    assert sorted(event["key_ids"]) == ["k1", "k2"]
    assert "added 1" in event["change"]
    assert "revoked 1" in event["change"]


def test_diff_trust_roots_initialized_when_no_prior():
    from services.review.trust_root import diff_trust_roots

    new = parse_trust_root(_bundle([{"id": "k1", "public_key": "ssh-ed25519 A"}]))
    change, affected = diff_trust_roots(None, new)
    assert change == "initialized"
    assert affected == ["k1"]
