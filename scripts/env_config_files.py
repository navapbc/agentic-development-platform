"""Write the environment's config files from PLATFORM_ENV_CONFIG into a directory.

A deployed task has no repository checkout and a read-only root, so Terraform passes the
operator's config/<environment> files as one JSON object of file name to content, and the
platform image's entrypoint writes them here before the service starts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ALLOWED_SUFFIXES = (".yaml", ".yml")


class EnvConfigError(Exception):
    """PLATFORM_ENV_CONFIG names a file this writer refuses."""


def write(files: dict[str, str], directory: Path) -> list[Path]:
    for name, content in files.items():
        if Path(name).name != name or name.startswith(".") or not name.endswith(ALLOWED_SUFFIXES):
            raise EnvConfigError(f"{name!r} is not a plain YAML file name")
        if not isinstance(content, str):
            raise EnvConfigError(f"{name} does not hold text")
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    written = []
    for name, content in sorted(files.items()):
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python -m scripts.env_config_files DIRECTORY", file=sys.stderr)
        return 2
    try:
        files = json.loads(os.environ.get("PLATFORM_ENV_CONFIG", "{}"))
        if not isinstance(files, dict):
            raise EnvConfigError("PLATFORM_ENV_CONFIG is not a JSON object")
        written = write(files, Path(arguments[0]))
    except (EnvConfigError, json.JSONDecodeError) as exc:
        print(f"[env-config] {exc}", file=sys.stderr)
        return 1
    print(f"[env-config] wrote {', '.join(path.name for path in written) or 'no files'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
