"""Tests for signing and verifying artifact authorship over the rebar substrate."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from services.orchestration.authorship import (
    AuthorshipConfigError,
    AuthorshipRecord,
    create_authoring_identity,
    sign_artifact_authorship,
    sign_authorship_event,
    verify_authorship_record,
)
from tests.support import hermetic_settings

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
requires_ssh_keygen = pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")


def _init_store(root: Path) -> None:
    import rebar

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "a@b.invalid"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "agent"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root), force_new_store=True)


def _keygen(tmp_path: Path, name: str) -> tuple[str, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", name, "-q"],
        check=True,
    )
    return str(key_path), (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()


def _setup(tmp_path: Path):
    store = tmp_path / "store"
    _init_store(store)
    key_path, pub = _keygen(tmp_path, "signer")
    identity_id = create_authoring_identity("code-agent", "code@nava", pub, repo_root=str(store))
    settings = hermetic_settings(
        ticket_backend="rebar",
        authorship_identity_id=identity_id,
        authorship_signing_key_path=key_path,
    )
    return store, settings, identity_id, key_path


@requires_ssh_keygen
def test_sign_and_verify_round_trip(tmp_path):
    store, settings, _, _ = _setup(tmp_path)
    for kind in ("plan", "code"):
        record = sign_artifact_authorship(
            execution_id="e1", kind=kind, artifact_hash="deadbeef", settings=settings
        )
        assert record is not None
        assert verify_authorship_record(record, repo_root=str(store)) is True


@requires_ssh_keygen
def test_tampered_event_fails_verification(tmp_path):
    store, settings, _, _ = _setup(tmp_path)
    record = sign_artifact_authorship(
        execution_id="e1", kind="code", artifact_hash="deadbeef", settings=settings
    )
    assert record is not None
    tampered = AuthorshipRecord(
        kind=record.kind,
        identity_id=record.identity_id,
        event={**record.event, "artifact_hash": "0000"},
        envelope=record.envelope,
    )
    assert verify_authorship_record(tampered, repo_root=str(store)) is False


@requires_ssh_keygen
def test_out_of_trust_root_key_fails_verification(tmp_path):
    store, _, identity_id, _ = _setup(tmp_path)
    other_key, _ = _keygen(tmp_path, "intruder")
    event = {"uuid": "e1:code", "kind": "code", "artifact_hash": "deadbeef", "commit": ""}
    envelope = sign_authorship_event(event, key_path=other_key, identity_id=identity_id)
    record = AuthorshipRecord(kind="code", identity_id=identity_id, event=event, envelope=envelope)
    assert verify_authorship_record(record, repo_root=str(store)) is False


@requires_ssh_keygen
def test_signing_is_noop_on_jira_backend(tmp_path):
    _, settings, identity_id, key_path = _setup(tmp_path)
    jira_settings = hermetic_settings(
        ticket_backend="jira",
        authorship_identity_id=identity_id,
        authorship_signing_key_path=key_path,
    )
    record = sign_artifact_authorship(
        execution_id="e1", kind="code", artifact_hash="deadbeef", settings=jira_settings
    )
    assert record is None


def test_signing_raises_on_missing_key_on_rebar_backend():
    settings = hermetic_settings(
        ticket_backend="rebar",
        authorship_identity_id="id-1",
        authorship_signing_key_path="/no/such/key",
    )
    with pytest.raises(AuthorshipConfigError):
        sign_artifact_authorship(
            execution_id="e1", kind="code", artifact_hash="x", settings=settings
        )


def test_records_serialize_round_trip():
    record = AuthorshipRecord(
        kind="plan", identity_id="id-1", event={"uuid": "e1:plan"}, envelope="{}"
    )
    assert AuthorshipRecord.from_dict(record.to_dict()) == record
