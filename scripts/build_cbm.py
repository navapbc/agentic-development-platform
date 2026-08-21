"""Build the codebase-memory-mcp binary and vendor it under vendor/bin.

The source defaults to the upstream repository at a pinned tag and is cloned into
a temporary directory, then built with docker/cbm-build.Dockerfile. Pass --source
to build from an existing local checkout instead, for a closed environment.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

DEFAULT_URL = os.environ.get(
    "CBM_SOURCE_URL", "https://github.com/DeusData/codebase-memory-mcp.git"
)
DEFAULT_REF = os.environ.get("CBM_SOURCE_REF", "v0.9.0")
DEFAULT_DEST = "vendor/bin"
_DOCKERFILE = "docker/cbm-build.Dockerfile"


def clone_command(url: str, ref: str, into: str) -> list[str]:
    return ["git", "clone", "--depth", "1", "--branch", ref, url, into]


def build_command(source: str, dest: str) -> list[str]:
    return [
        "docker",
        "build",
        "-f",
        _DOCKERFILE,
        "--target",
        "export",
        "--output",
        f"type=local,dest={dest}",
        source,
    ]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def build(
    *,
    source: str | None,
    url: str,
    ref: str,
    dest: str,
    runner=_run,
) -> str:
    """Build CBM into ``dest``. Clone upstream unless a local source is given."""
    if source:
        runner(build_command(source, dest))
        return str(Path(dest) / "codebase-memory-mcp")
    with tempfile.TemporaryDirectory(prefix="cbm-src-") as tmp:
        runner(clone_command(url, ref, tmp))
        runner(build_command(tmp, dest))
    return str(Path(dest) / "codebase-memory-mcp")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and vendor the codebase-memory-mcp binary")
    parser.add_argument("--source", default=None, help="Existing CBM source checkout to build from")
    parser.add_argument("--url", default=DEFAULT_URL, help="Upstream CBM git URL")
    parser.add_argument("--ref", default=DEFAULT_REF, help="Upstream tag, branch, or commit")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="Output directory for the binary")
    args = parser.parse_args()

    Path(args.dest).mkdir(parents=True, exist_ok=True)
    out = build(source=args.source, url=args.url, ref=args.ref, dest=args.dest)
    print(f"vendored {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
