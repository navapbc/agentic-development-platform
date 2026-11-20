"""Write settings into the workstation's .env, keeping every other line as it is."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def set_values(values: Mapping[str, str], path: Path = ENV_FILE) -> None:
    """Replace each key's line in the file, or append the key when the file lacks it."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines += [f"{key}={value}" for key, value in remaining.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
