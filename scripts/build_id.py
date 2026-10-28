"""Build identifier derivation for Temporal worker versioning.

`services.orchestration.suspension.is_newer_build` orders two build identifiers by the tuple
of digit runs it reads out of each one, so a newer build is recognised as newer only when its
identifier grows numerically. The release version followed by a UTC timestamp holds under that
parser, and a commit SHA does not, because hex is not reliably numeric and carries no order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

DISTRIBUTION = "agentic-dev-platform"
_STAMP_FORMAT = "%Y%m%d%H%M%S"
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


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


def main() -> int:
    print(generate_build_id())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
