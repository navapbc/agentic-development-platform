"""Integration scenario for authored-identity signing at the activity layer.

Exercises the sign and merge-gate activities against a real rebar store and real
ssh-keygen, with no model calls. The full Temporal-plus-Bedrock workflow run is
covered by the batch end-to-end.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from services.orchestration import authorship as lib
from services.orchestration.temporal_activities import authorship as acts
from tests.support import hermetic_settings

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
pytestmark = pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")


def _init_store(root: Path) -> None:
    import rebar

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "agent"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root), force_new_store=True)


def _keygen(tmp_path: Path, name: str) -> tuple[str, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", name, "-q"], check=True
    )
    return str(key_path), (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()


@pytest.fixture
def rebar_signing(tmp_path, monkeypatch):
    store = tmp_path / "store"
    _init_store(store)
    key_path, pub = _keygen(tmp_path, "signer")
    identity_id = lib.create_authoring_identity(
        "code-agent", "code@nava", pub, repo_root=str(store)
    )
    settings = hermetic_settings(
        ticket_backend="rebar",
        authorship_identity_id=identity_id,
        authorship_signing_key_path=key_path,
    )
    monkeypatch.setattr(lib, "get_settings", lambda: settings)
    monkeypatch.setattr(lib, "store_repo_root", lambda *a, **k: str(store))
    monkeypatch.setattr(acts, "store_repo_root", lambda *a, **k: str(store))
    return settings, str(store)


async def test_sign_then_merge_gate_admits(rebar_signing):
    records = []
    for kind in ("plan", "code"):
        record = await acts.sign_authorship(
            acts.SignAuthorshipRequest(execution_id="e1", kind=kind, artifact=f"{kind}-body")
        )
        assert record is not None
        records.append(record)
    await acts.verify_authorship_merge(acts.VerifyAuthorshipRequest(records=records))


async def test_merge_gate_blocks_a_tampered_signature(rebar_signing):
    plan = await acts.sign_authorship(
        acts.SignAuthorshipRequest(execution_id="e1", kind="plan", artifact="plan-body")
    )
    code = await acts.sign_authorship(
        acts.SignAuthorshipRequest(execution_id="e1", kind="code", artifact="code-body")
    )
    assert plan is not None and code is not None
    code["event"]["artifact_hash"] = "0" * 64
    with pytest.raises(lib.AuthorshipVerificationError):
        await acts.verify_authorship_merge(acts.VerifyAuthorshipRequest(records=[plan, code]))


async def test_jira_backend_signs_nothing_and_gate_is_inert(monkeypatch):
    settings = hermetic_settings(ticket_backend="jira", authorship_identity_id="id-1")
    monkeypatch.setattr(lib, "get_settings", lambda: settings)
    record = await acts.sign_authorship(
        acts.SignAuthorshipRequest(execution_id="e1", kind="code", artifact="code-body")
    )
    assert record is None
    await acts.verify_authorship_merge(acts.VerifyAuthorshipRequest(records=[]))


def _record_signed_by(key_path: str, identity_id: str, kind: str) -> dict:
    event = lib.build_authorship_event(
        execution_id="e1", kind=kind, artifact_hash=f"{kind}-body", commit=""
    )
    envelope = lib.sign_authorship_event(event, key_path=key_path, identity_id=identity_id)
    return lib.AuthorshipRecord(
        kind=kind, identity_id=identity_id, event=event, envelope=envelope
    ).to_dict()


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(event)


async def test_merge_gate_admits_records_signed_by_a_rotated_key(rebar_signing, tmp_path):
    settings, store = rebar_signing
    identity_id = settings.authorship_identity_id
    genesis_key = settings.authorship_signing_key_path
    new_key, new_pub = _keygen(tmp_path, "rotated")
    lib.rotate_identity_key(
        identity_id,
        new_pub,
        current_key_path=genesis_key,
        repo_root=store,
        audit=_CollectingSink(),
    )

    records = [
        _record_signed_by(new_key, identity_id, "plan"),
        _record_signed_by(new_key, identity_id, "code"),
    ]
    await acts.verify_authorship_merge(acts.VerifyAuthorshipRequest(records=records))


async def test_merge_gate_blocks_records_signed_by_a_revoked_key(rebar_signing, tmp_path):
    settings, store = rebar_signing
    identity_id = settings.authorship_identity_id
    genesis_key = settings.authorship_signing_key_path
    genesis_pub = lib.public_key_line(genesis_key)
    new_key, new_pub = _keygen(tmp_path, "rotated")
    audit = _CollectingSink()
    lib.rotate_identity_key(
        identity_id, new_pub, current_key_path=genesis_key, repo_root=store, audit=audit
    )
    lib.revoke_identity_key(
        identity_id, genesis_pub, current_key_path=new_key, repo_root=store, audit=audit
    )

    revoked_records = [
        _record_signed_by(genesis_key, identity_id, "plan"),
        _record_signed_by(genesis_key, identity_id, "code"),
    ]
    with pytest.raises(lib.AuthorshipVerificationError):
        await acts.verify_authorship_merge(acts.VerifyAuthorshipRequest(records=revoked_records))

    current_records = [
        _record_signed_by(new_key, identity_id, "plan"),
        _record_signed_by(new_key, identity_id, "code"),
    ]
    await acts.verify_authorship_merge(acts.VerifyAuthorshipRequest(records=current_records))
