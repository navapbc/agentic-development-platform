"""Per-environment config loading and secret resolution behind one interface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when a per-environment config file is missing or malformed."""


class SecretNotFoundError(KeyError):
    """Raised when a secret reference resolves to no value."""

    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref

    def __str__(self) -> str:
        return f"secret reference not found: {self.ref}"


@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str: ...


class EnvSecretResolver:
    """Resolve a reference from the process environment merged over the local .env."""

    def __init__(
        self,
        source: Mapping[str, str] | None = None,
        *,
        env_file: Path | None = None,
    ) -> None:
        self._source = source
        self._env_file = env_file if env_file is not None else _REPO_ROOT / ".env"

    def resolve(self, ref: str) -> str:
        source = self._source if self._source is not None else self._load()
        value = source.get(ref)
        if not value:
            raise SecretNotFoundError(ref)
        return value

    def _load(self) -> Mapping[str, str]:
        merged: dict[str, str] = {}
        if self._env_file.exists():
            for key, value in dotenv_values(self._env_file).items():
                if value is not None:
                    merged[key] = value
        merged.update(os.environ)
        return merged


def load_env_config(name: str, *, environment: str, root: Path | None = None) -> dict[str, Any]:
    """Load config/<environment>/<name>.yaml as a mapping."""
    base = root if root is not None else _REPO_ROOT
    path = base / "config" / environment / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"config file missing: {path}; copy {path.name}.example to create it")
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return parsed
