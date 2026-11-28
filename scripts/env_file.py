"""Locate the workstation's settings files, and write settings into the platform settings file.

config/local/platform.env holds the platform settings, and config/local/tenants/<tenant>.env
holds the settings of one tenant. Each path is relative to the checkout root.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_ENV_FILE = Path("config") / "local" / "platform.env"
TENANTS_DIRECTORY = Path("config") / "local" / "tenants"


def platform_env_path(root: Path | None = None) -> Path:
    return (root or CHECKOUT_ROOT) / PLATFORM_ENV_FILE


def platform_env_values() -> dict[str, str | None]:
    from dotenv import dotenv_values

    return dotenv_values(platform_env_path())


def tenant_env_path(tenant: str, root: Path | None = None) -> Path:
    return (root or CHECKOUT_ROOT) / TENANTS_DIRECTORY / f"{tenant}.env"


def set_values(values: Mapping[str, str], path: Path | None = None) -> None:
    """Replace each key's line in the file, or append the key when the file lacks it.

    The file is the platform settings file unless path names another.
    """
    target = path or platform_env_path()
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines += [f"{key}={value}" for key, value in remaining.items()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
