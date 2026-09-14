"""Tests for merge-gate authorship verification, failing closed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from services.orchestration.authorship import (
    AuthorshipRecord,
    AuthorshipVerificationError,
    create_authoring_identity,
    sign_artifact_authorship,
    verify_authorship_at_merge,
)
from tests.support import hermetic_settings

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
requires_ssh_keygen = pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")


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
    return store, settings


def _records(settings) -> list[AuthorshipRecord]:
    return [
        sign_artifact_authorship(
            execution_id="e1", kind=kind, artifact_hash=f"{kind}-hash", settings=settings
        )
        for kind in ("plan", "code")
    ]  # type: ignore[misc]


@requires_ssh_keygen
def test_both_valid_signatures_pass(tmp_path):
    store, settings = _setup(tmp_path)
    verify_authorship_at_merge(_records(settings), repo_root=str(store), settings=settings)


@requires_ssh_keygen
def test_missing_signature_blocks(tmp_path):
    store, settings = _setup(tmp_path)
    only_plan = _records(settings)[:1]
    with pytest.raises(AuthorshipVerificationError):
        verify_authorship_at_merge(only_plan, repo_root=str(store), settings=settings)


@requires_ssh_keygen
def test_tampered_signature_blocks(tmp_path):
    store, settings = _setup(tmp_path)
    records = _records(settings)
    records[1] = AuthorshipRecord(
        kind=records[1].kind,
        identity_id=records[1].identity_id,
        event={**records[1].event, "artifact_hash": "tampered"},
        envelope=records[1].envelope,
    )
    with pytest.raises(AuthorshipVerificationError):
        verify_authorship_at_merge(records, repo_root=str(store), settings=settings)


@requires_ssh_keygen
def test_empty_records_block_on_active_backend(tmp_path):
    store, settings = _setup(tmp_path)
    with pytest.raises(AuthorshipVerificationError):
        verify_authorship_at_merge([], repo_root=str(store), settings=settings)


def test_noop_on_jira_backend():
    settings = hermetic_settings(ticket_backend="jira", authorship_identity_id="id-1")
    verify_authorship_at_merge([], repo_root="/tmp/does-not-matter", settings=settings)
