"""The pinned Amazon Linux 2023 base and toolchain versions every platform Dockerfile builds on.

`docker/al2023/versions.env` holds each pin once. A Dockerfile repeats the pins it uses as `ARG`
defaults, because a Dockerfile cannot read a file before its first `FROM`, and the image content
hash the publish task tags with covers those defaults, so a changed pin publishes a new image.
`check` reports a Dockerfile whose defaults drifted from the file, and `update` rewrites them.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "docker" / "al2023" / "versions.env"
DOCKERFILE_GLOBS = (
    "docker/*.Dockerfile",
    "docker/*/Dockerfile",
    "examples/gate-engines/*/Dockerfile",
)
_ARG = re.compile(r"^(?P<lead>[ \t]*ARG[ \t]+)(?P<name>[A-Z0-9_]+)=(?P<value>\S*)[ \t]*$", re.M)


def load(path: Path = VERSIONS_FILE) -> dict[str, str]:
    """Return the pins in the versions file, in file order."""
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Z0-9_]+", name):
            raise ValueError(f"{path.name} line {number} is not NAME=value")
        values[name] = value
    return values


def dockerfiles(root: Path = REPO_ROOT) -> list[Path]:
    return sorted({path for pattern in DOCKERFILE_GLOBS for path in root.glob(pattern)})


def drift(pins: Mapping[str, str], paths: Iterable[Path]) -> list[str]:
    """Return each ARG default that names a pin and holds a different value."""
    found = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in _ARG.finditer(text):
            name, value = match.group("name"), match.group("value")
            if name in pins and value != pins[name]:
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.name} line {line} sets {name}={value}, not {pins[name]}")
    return found


def update(pins: Mapping[str, str], paths: Iterable[Path]) -> list[Path]:
    """Rewrite each ARG default that names a pin to the pinned value, returning changed files."""
    changed = []
    for path in paths:
        text = path.read_text(encoding="utf-8")

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            if name not in pins:
                return match.group(0)
            return f"{match.group('lead')}{name}={pins[name]}"

        rewritten = _ARG.sub(replace, text)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Rewrite drifted ARG defaults.")
    args = parser.parse_args(argv)
    pins = load()
    if args.update:
        for path in update(pins, dockerfiles()):
            print(f"[base-versions] updated {path.relative_to(REPO_ROOT)}")
        return 0
    problems = drift(pins, dockerfiles())
    for problem in problems:
        print(f"[base-versions] {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
