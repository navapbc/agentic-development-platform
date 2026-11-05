"""Tests for resolving an SSH signing key from an injected value or a file path."""

from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from scripts.config import Settings
from services.orchestration import signing_keys
from services.orchestration.authorship import signing_key_path
from services.orchestration.review_signing import (
    ReviewSigningConfigError,
    review_signing_preflight,
)
from services.orchestration.signing_keys import (
    SigningKeyError,
    materialize_signing_key,
    resolve_signing_key_path,
)

requires_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen must be on PATH"
)


def _keypair(tmp_path: Path) -> tuple[str, str]:
    key_path = tmp_path / "signer"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "", "-q"],
        check=True,
    )
    return key_path.read_text(encoding="utf-8"), (tmp_path / "signer.pub").read_text(
        encoding="utf-8"
    )


def test_path_is_used_when_no_key_is_injected():
    assert resolve_signing_key_path("", "config/local/review-signing-key") == (
        "config/local/review-signing-key"
    )


@requires_ssh_keygen
def test_injected_key_is_written_private_with_its_public_half(tmp_path):
    private_key, public_key = _keypair(tmp_path)

    resolved = Path(resolve_signing_key_path(private_key, "ignored-path"))

    assert resolved.read_text(encoding="utf-8") == private_key
    assert stat.S_IMODE(resolved.stat().st_mode) == 0o600
    derived = Path(f"{resolved}.pub").read_text(encoding="utf-8").split()[:2]
    assert derived == public_key.split()[:2]


@requires_ssh_keygen
def test_injected_key_is_written_once_per_process(tmp_path):
    private_key, _ = _keypair(tmp_path)

    assert materialize_signing_key(private_key) == materialize_signing_key(private_key.strip())


@requires_ssh_keygen
def test_invalid_injected_key_raises_and_leaves_no_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with pytest.raises(SigningKeyError):
        materialize_signing_key("not a private key")

    assert list(tmp_path.iterdir()) == []


@requires_ssh_keygen
def test_materialized_key_is_removed_when_its_directory_is_released(tmp_path):
    private_key, _ = _keypair(tmp_path)
    key_file = Path(materialize_signing_key(private_key))

    for holder in signing_keys._key_directories:
        holder.cleanup()

    assert not key_file.exists()
    assert not key_file.parent.exists()


@requires_ssh_keygen
def test_concurrent_callers_share_one_materialized_key(tmp_path):
    private_key, _ = _keypair(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = set(pool.map(lambda _: materialize_signing_key(private_key + " "), range(16)))

    assert len(paths) == 1


@requires_ssh_keygen
def test_authorship_key_resolves_from_injected_value(tmp_path):
    private_key, _ = _keypair(tmp_path)

    resolved = signing_key_path(Settings(authorship_signing_key=private_key))

    assert Path(resolved).read_text(encoding="utf-8") == private_key


@requires_ssh_keygen
def test_review_preflight_reports_an_invalid_injected_key(tmp_path):
    trust_path = tmp_path / "trust-root.yaml"
    trust_path.write_text("version: 1\n", encoding="utf-8")
    settings = Settings(
        review_signing_enabled=True,
        review_signing_key="not a private key",
        review_trust_root_path=str(trust_path),
    )

    with pytest.raises(ReviewSigningConfigError):
        review_signing_preflight(settings)
