"""Tests for authored-identity provisioning over the rebar authorship substrate."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from services.orchestration.authorship import (
    AuthorshipConfigError,
    authorship_signing_active,
    create_authoring_identity,
    provision_authoring_identity,
    public_key_line,
    resolve_authoring_trust_root,
    seed_genesis_key,
    signing_key_path,
)
from tests.support import hermetic_settings

_HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
requires_ssh_keygen = pytest.mark.skipif(not _HAS_SSH_KEYGEN, reason="ssh-keygen must be on PATH")


def _init_store(root: Path) -> None:
    import rebar

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "agent-platform@users.noreply.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "agent-platform"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root), force_new_store=True)


def _keygen(tmp_path: Path, name: str = "signer") -> tuple[str, str]:
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "author@nava", "-q"],
        check=True,
    )
    public_line = (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip()
    return str(key_path), public_line


def test_authorship_signing_active_only_on_rebar_with_identity():
    assert authorship_signing_active(
        hermetic_settings(ticket_backend="rebar", authorship_identity_id="id-1")
    )
    assert not authorship_signing_active(
        hermetic_settings(ticket_backend="jira", authorship_identity_id="id-1")
    )
    assert not authorship_signing_active(
        hermetic_settings(ticket_backend="rebar", authorship_identity_id="")
    )
    assert not authorship_signing_active(
        hermetic_settings(
            ticket_backend="rebar", authorship_identity_id="id-1", authorship_signing_enabled=False
        )
    )


@requires_ssh_keygen
def test_create_authoring_identity_seeds_genesis_and_resolves_trust_root(tmp_path):
    _init_store(tmp_path / "store")
    _, public_line = _keygen(tmp_path)
    identity_id = create_authoring_identity(
        "code-agent", "code-agent@nava", public_line, repo_root=str(tmp_path / "store")
    )
    trust_root = resolve_authoring_trust_root(identity_id, repo_root=str(tmp_path / "store"))
    assert identity_id in trust_root
    assert public_line.split()[1] in trust_root


@requires_ssh_keygen
def test_seed_genesis_key_is_idempotent(tmp_path):
    store = tmp_path / "store"
    _init_store(store)
    _, public_line = _keygen(tmp_path)
    identity_id = create_authoring_identity(
        "code-agent", "code-agent@nava", public_line, repo_root=str(store)
    )
    assert seed_genesis_key(identity_id, public_line, repo_root=str(store)) is False


@requires_ssh_keygen
def test_second_unsigned_key_add_is_refused(tmp_path):
    import rebar
    from rebar._errors import RebarError

    store = tmp_path / "store"
    _init_store(store)
    _, first_pub = _keygen(tmp_path, "first")
    _, second_pub = _keygen(tmp_path, "second")
    identity_id = create_authoring_identity(
        "code-agent", "code-agent@nava", first_pub, repo_root=str(store)
    )
    with pytest.raises(RebarError):
        rebar.add_identity_key(identity_id, second_pub, repo_root=str(store))


def test_signing_key_path_missing_raises():
    settings = hermetic_settings(authorship_signing_key_path="/no/such/key")
    with pytest.raises(AuthorshipConfigError):
        signing_key_path(settings)


def test_public_key_line_missing_pub_raises(tmp_path):
    key = tmp_path / "signer"
    key.write_text("private", encoding="utf-8")
    with pytest.raises(AuthorshipConfigError):
        public_key_line(str(key))


@requires_ssh_keygen
def test_provision_resolves_existing_identity_and_seeds(tmp_path):
    import rebar

    store = tmp_path / "store"
    _init_store(store)
    key_path, _ = _keygen(tmp_path)
    identity_id = str(rebar.create_identity("code-agent", "code-agent@nava", repo_root=str(store)))
    settings = hermetic_settings(
        ticket_backend="rebar",
        authorship_identity_id=identity_id,
        authorship_signing_key_path=key_path,
    )
    resolved = provision_authoring_identity(settings, repo_root=str(store))
    assert resolved == identity_id
    assert identity_id in resolve_authoring_trust_root(identity_id, repo_root=str(store))


@requires_ssh_keygen
def test_provision_raises_when_identity_absent(tmp_path):
    store = tmp_path / "store"
    _init_store(store)
    key_path, _ = _keygen(tmp_path)
    settings = hermetic_settings(
        ticket_backend="rebar",
        authorship_identity_id="0000000000000000",
        authorship_signing_key_path=key_path,
    )
    with pytest.raises(AuthorshipConfigError):
        provision_authoring_identity(settings, repo_root=str(store))
