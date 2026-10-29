"""Build identifier derivation for Temporal worker versioning.

`services.orchestration.suspension.is_newer_build` orders two build identifiers by the tuple
of digit runs it reads out of each one, so a newer build is recognised as newer only when its
identifier grows numerically. The release version followed by a UTC timestamp holds under that
parser, and a commit SHA does not, because hex is not reliably numeric and carries no order.

A content digest cannot serve as the identifier for the same reason, so the digest of the
image sources selects an identifier through a ledger instead. Unchanged sources find their
recorded identifier and reuse it, and changed sources mint one carrying the current timestamp.
A build therefore consumes a Temporal deployment version only when its image content differs
from the previous build.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

DISTRIBUTION = "agentic-dev-platform"
LEDGER_NAME = ".build-id.json"
_STAMP_FORMAT = "%Y%m%d%H%M%S"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_EXCLUDED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".pyd")

IMAGE_SOURCE_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "NOTICE",
    "alembic.ini",
    "services",
    "adapters",
    "schemas",
    "database",
    "scripts",
    "observability",
    "openhands",
    "docker/Dockerfile",
)


def release_version() -> str:
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return _pyproject_version()


def _pyproject_version() -> str:
    import tomllib

    with _PYPROJECT.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def generate_build_id(moment: datetime | None = None) -> str:
    stamp = (moment or datetime.now(UTC)).strftime(_STAMP_FORMAT)
    return f"{release_version()}.{stamp}"


def _image_source_files(root: Path) -> Iterator[Path]:
    for entry in IMAGE_SOURCE_PATHS:
        path = root / entry
        if path.is_file():
            yield path
            continue
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            if _EXCLUDED_DIRS.intersection(child.parts):
                continue
            if child.suffix in _EXCLUDED_SUFFIXES:
                continue
            yield child


def source_digest(root: Path | None = None) -> str:
    """Digest the sources the worker image is built from, stable across a rebuild."""
    root = (root or _REPO_ROOT).resolve()
    digest = hashlib.sha256()
    for path in sorted(_image_source_files(root)):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _read_ledger(root: Path) -> dict[str, str]:
    try:
        entry = json.loads((root / LEDGER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return entry if isinstance(entry, dict) else {}


def resolve_build_id(root: Path | None = None, moment: datetime | None = None) -> str:
    """Return the identifier of the image these sources build, minting one when they change."""
    root = (root or _REPO_ROOT).resolve()
    digest = source_digest(root)
    recorded = _read_ledger(root)
    if recorded.get("digest") == digest and recorded.get("build_id"):
        return recorded["build_id"]
    build_id = generate_build_id(moment)
    (root / LEDGER_NAME).write_text(
        json.dumps({"digest": digest, "build_id": build_id}, indent=2) + "\n", encoding="utf-8"
    )
    return build_id


def recorded_build_id(root: Path | None = None) -> str:
    """Return the identifier the last build recorded, falling back to the release version.

    Host tasks compare against the build the workers run, so this reads the ledger without
    digesting the sources. A checkout whose sources have moved on since the last build still
    reports the identifier that is deployed.
    """
    return _read_ledger((root or _REPO_ROOT).resolve()).get("build_id") or release_version()


def main() -> int:
    print(resolve_build_id())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
