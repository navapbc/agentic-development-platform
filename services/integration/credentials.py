"""Per-environment config loading and secret resolution behind one interface."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from dotenv import dotenv_values

from services.operator_remedy import RemediableError

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_VERSION_KEY = "config_version"

_CURRENT_CONFIG_VERSIONS: dict[str, int] = {
    "github": 1,
    "jira": 1,
    "integrations": 1,
    "gates": 1,
    "gate_engine": 1,
    "checks": 1,
    "tickets": 1,
}


def current_config_version(name: str, *, versions: Mapping[str, int] | None = None) -> int:
    """Return the config version this release expects for name, from a code constant."""
    table = versions if versions is not None else _CURRENT_CONFIG_VERSIONS
    try:
        return table[name]
    except KeyError:
        raise ConfigError(f"no current config version registered for {name!r}") from None


class ConfigError(RemediableError, RuntimeError):
    """Raised when a per-environment config file is missing or malformed.

    Validation failures name the offending key and need no remedy beyond that. Sites whose fix is
    a command or a file the operator has to create pass ``remedy``.
    """

    def __init__(self, message: str, *, remedy: str = "") -> None:
        super().__init__(message)
        self.remedy = remedy


class SecretNotFoundError(RemediableError, KeyError):
    """Raised when a secret reference resolves to no value."""

    def __init__(self, ref: str, *, env_file: Path | None = None) -> None:
        super().__init__(ref)
        self.ref = ref
        self.env_file = env_file
        self.remedy = (
            f"Export {ref}, or add a {ref}= line to the .env file, then restart the stack with "
            "`inv local-up` so the workers pick it up."
        )

    def condition(self) -> str:
        where = f" or in {self.env_file}" if self.env_file is not None else ""
        return f"the secret {self.ref} is not set in the process environment{where}."


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
            raise SecretNotFoundError(ref, env_file=self._env_file)
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
        raise ConfigError(
            f"config file {path} does not exist.",
            remedy=f"Copy {path.name}.example to {path.name} in that directory, then fill in the "
            "environment values.",
        )
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return parsed


def load_platform_config(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """Load config/platform/<name>.yaml, the tracked configuration shared by every environment."""
    base = root if root is not None else _REPO_ROOT
    path = base / "config" / "platform" / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"platform config file missing: {path}")
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return parsed


def _read_config_file(
    name: str, *, environment: str, root: Path | None, suffix: str
) -> dict[str, Any] | None:
    base = root if root is not None else _REPO_ROOT
    path = base / "config" / environment / f"{name}.yaml{suffix}"
    if not path.exists():
        return None
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return parsed


def config_version(raw: Mapping[str, Any]) -> int:
    value = raw.get(CONFIG_VERSION_KEY, 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{CONFIG_VERSION_KEY} must be an integer")
    return value


def parse_allowlist(raw: Mapping[str, Any], source: str) -> tuple[str, ...]:
    """Parse a top-level ``allowlist`` list of names from a per-source config mapping."""
    value = raw.get("allowlist") or []
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ConfigError(f"{source} config 'allowlist' is not a list")
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str | int) or isinstance(entry, bool):
            raise ConfigError(f"{source} config allowlist entry is not a scalar")
        text = str(entry).strip()
        if text:
            items.append(text)
    return tuple(items)


@dataclass(frozen=True)
class ConfigStatus:
    name: str
    present: bool
    operator_version: int
    current_version: int
    example_version: int
    example_matches_current: bool
    up_to_date: bool
    new_keys: tuple[str, ...]


def config_status(
    name: str, *, environment: str, root: Path | None = None, current: int | None = None
) -> ConfigStatus:
    current_version = current if current is not None else current_config_version(name)
    example = _read_config_file(name, environment=environment, root=root, suffix=".example")
    if example is None:
        raise ConfigError(f"missing committed template config/{environment}/{name}.yaml.example")
    example_version = config_version(example)
    example_matches_current = example_version == current_version
    operator = _read_config_file(name, environment=environment, root=root, suffix="")
    if operator is None:
        return ConfigStatus(
            name=name,
            present=False,
            operator_version=0,
            current_version=current_version,
            example_version=example_version,
            example_matches_current=example_matches_current,
            up_to_date=False,
            new_keys=tuple(k for k in sorted(example) if k != CONFIG_VERSION_KEY),
        )
    operator_version = config_version(operator)
    new_keys = tuple(k for k in sorted(set(example) - set(operator)) if k != CONFIG_VERSION_KEY)
    return ConfigStatus(
        name=name,
        present=True,
        operator_version=operator_version,
        current_version=current_version,
        example_version=example_version,
        example_matches_current=example_matches_current,
        up_to_date=operator_version >= current_version,
        new_keys=new_keys,
    )


def require_current_config(
    name: str, *, environment: str, root: Path | None = None, current: int | None = None
) -> None:
    current_version = current if current is not None else current_config_version(name)
    operator = _read_config_file(name, environment=environment, root=root, suffix="")
    if operator is None:
        return
    operator_version = config_version(operator)
    if operator_version >= current_version:
        return
    raise ConfigError(
        f"config/{environment}/{name}.yaml is version {operator_version} but this release "
        f"expects version {current_version}.",
        remedy="Run `inv config-check` and add the new keys to the config file.",
    )
