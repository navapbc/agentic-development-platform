"""Resolve an SSH signing key from an environment value or a file path.

A deployed environment injects each private signing key from the secret store as an
environment value. ssh-keygen signs from a file, so an injected key is written once
per process to a private file, with its public half alongside as ``<path>.pub``. The file
lives in a temporary directory that is removed when the process exits.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from scripts.config import Settings

from services import process

_materialized: dict[str, str] = {}
_key_directories: list[tempfile.TemporaryDirectory[str]] = []
_lock = threading.Lock()


class SigningKeyError(RuntimeError):
    """An injected signing key could not be written or is not a valid private key."""


def review_signing_key_path(settings: Settings) -> str:
    return resolve_signing_key_path(settings.review_signing_key, settings.review_signing_key_path)


def authorship_signing_key_path(settings: Settings) -> str:
    return resolve_signing_key_path(
        settings.authorship_signing_key, settings.authorship_signing_key_path
    )


def resolve_signing_key_path(private_key: str, key_path: str) -> str:
    """Return a file holding the signing key. An injected key takes precedence over a path."""
    if not private_key.strip():
        return key_path
    return materialize_signing_key(private_key)


def materialize_signing_key(private_key: str) -> str:
    content = private_key.strip() + "\n"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _lock:
        cached = _materialized.get(digest)
        if cached is not None and Path(cached).is_file():
            return cached
        holder = tempfile.TemporaryDirectory(prefix="signing-key-")
        try:
            key_file = _write_key_pair(Path(holder.name), content)
        except BaseException:
            holder.cleanup()
            raise
        _key_directories.append(holder)
        _materialized[digest] = str(key_file)
        return str(key_file)


def _write_key_pair(directory: Path, content: str) -> Path:
    key_file = directory / "key"
    descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    try:
        derived = process.run(
            ["ssh-keygen", "-y", "-f", str(key_file)],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, process.SubprocessError) as exc:
        raise SigningKeyError(
            "the injected signing key is not a readable OpenSSH private key, "
            "or ssh-keygen is not on PATH"
        ) from exc
    (directory / "key.pub").write_text(derived.stdout, encoding="utf-8")
    return key_file
