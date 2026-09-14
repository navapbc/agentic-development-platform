"""Tests for authorship key rotation, revocation, and audit over the rebar keyring."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from services.orchestration.authorship import (
    AuthorshipRecord,
    create_authoring_identity,
    revoke_identity_key,
    rotate_identity_key,
    sign_authorship_event,
    verify_authorship_record,
)

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
requires_ssh_keygen = pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(event)


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


def _record_signed_by(key_path: str, identity_id: str) -> AuthorshipRecord:
    event = {"uuid": "e1:code", "kind": "code", "artifact_hash": "deadbeef", "commit": ""}
    envelope = sign_authorship_event(event, key_path=key_path, identity_id=identity_id)
    return AuthorshipRecord(kind="code", identity_id=identity_id, event=event, envelope=envelope)


@requires_ssh_keygen
def test_rotation_admits_new_key_and_audits(tmp_path):
    store = tmp_path / "store"
    _init_store(store)
    genesis_key, genesis_pub = _keygen(tmp_path, "genesis")
    new_key, new_pub = _keygen(tmp_path, "rotated")
    identity_id = create_authoring_identity("a", "a@nava", genesis_pub, repo_root=str(store))

    sink = _CollectingSink()
    rotate_identity_key(
        identity_id, new_pub, current_key_path=genesis_key, repo_root=str(store), audit=sink
    )

    assert verify_authorship_record(
        _record_signed_by(new_key, identity_id), repo_root=str(store)
    )
    assert verify_authorship_record(
        _record_signed_by(genesis_key, identity_id), repo_root=str(store)
    )
    assert sink.events[-1]["op"] == "KEY_ADD"
    assert sink.events[-1]["identity_id"] == identity_id


@requires_ssh_keygen
def test_rotation_without_valid_signer_is_refused(tmp_path):
    from rebar._errors import RebarError

    store = tmp_path / "store"
    _init_store(store)
    genesis_key, genesis_pub = _keygen(tmp_path, "genesis")
    intruder_key, _ = _keygen(tmp_path, "intruder")
    _, new_pub = _keygen(tmp_path, "rotated")
    identity_id = create_authoring_identity("a", "a@nava", genesis_pub, repo_root=str(store))

    sink = _CollectingSink()
    with pytest.raises(RebarError):
        rotate_identity_key(
            identity_id, new_pub, current_key_path=intruder_key, repo_root=str(store), audit=sink
        )
    assert sink.events == []


@requires_ssh_keygen
def test_revocation_stops_verification_and_audits(tmp_path):
    store = tmp_path / "store"
    _init_store(store)
    genesis_key, genesis_pub = _keygen(tmp_path, "genesis")
    new_key, new_pub = _keygen(tmp_path, "rotated")
    identity_id = create_authoring_identity("a", "a@nava", genesis_pub, repo_root=str(store))

    sink = _CollectingSink()
    rotate_identity_key(
        identity_id, new_pub, current_key_path=genesis_key, repo_root=str(store), audit=sink
    )
    revoke_identity_key(
        identity_id, genesis_pub, current_key_path=new_key, repo_root=str(store), audit=sink
    )

    assert not verify_authorship_record(
        _record_signed_by(genesis_key, identity_id), repo_root=str(store)
    )
    assert verify_authorship_record(_record_signed_by(new_key, identity_id), repo_root=str(store))
    assert sink.events[-1]["op"] == "KEY_REVOKE"
