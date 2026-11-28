"""Config loading and secret resolution behind one interface."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_VERSION_KEY = "config_version"

_CURRENT_CONFIG_VERSIONS: dict[str, int] = {
    "github": 2,
    "jira": 1,
    "integrations": 1,
    "gates": 1,
    "gate_engine": 1,
    "checks": 1,
    "tickets": 1,
    "tickets_isolation": 1,
    "repositories": 1,
    "registries": 1,
    "profiles": 1,
    "image_policy": 1,
    "image_exceptions": 1,
    "escalation": 1,
    "slack_shared_app": 1,
}


def current_config_version(name: str, *, versions: Mapping[str, int] | None = None) -> int:
    """Return the config version this release expects for name, from a code constant."""
    table = versions if versions is not None else _CURRENT_CONFIG_VERSIONS
    try:
        return table[name]
    except KeyError:
        raise ConfigError(f"no current config version registered for {name!r}") from None


class ConfigError(RuntimeError):
    """Raised when a config file is missing or malformed."""


class SecretNotFoundError(KeyError):
    """Raised when a secret reference resolves to no value."""

    def __init__(self, ref: str, *, env_files: Sequence[Path] = ()) -> None:
        super().__init__(ref)
        self.ref = ref
        self.env_files = tuple(env_files)

    def __str__(self) -> str:
        where = "".join(f" or in {path}" for path in self.env_files)
        return (
            f"the secret {self.ref} is not set in the process environment{where}. "
            f"Export {self.ref}, or add a {self.ref}= line to config/local/platform.env for a "
            "platform secret or to config/local/tenants/<tenant>.env for a tenant secret, then "
            "restart the stack with inv local-up so the workers pick it up."
        )


@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str: ...


class EnvSecretResolver:
    """Resolve a reference from the process environment merged over the settings files.

    The settings files default to config/local/platform.env and, when TENANT names a tenant,
    config/local/tenants/<tenant>.env, which replaces the platform values.
    """

    def __init__(
        self,
        source: Mapping[str, str] | None = None,
        *,
        env_file: Path | None = None,
    ) -> None:
        self._source = source
        if env_file is not None:
            self._env_files: tuple[Path, ...] = (env_file,)
        else:
            from scripts.config import settings_files

            self._env_files = settings_files()

    def resolve(self, ref: str) -> str:
        source = self._source if self._source is not None else self._load()
        value = source.get(ref)
        if not value:
            raise SecretNotFoundError(ref, env_files=self._env_files)
        return value

    def _load(self) -> Mapping[str, str]:
        merged: dict[str, str] = {}
        for path in self._env_files:
            if path.exists():
                for key, value in dotenv_values(path).items():
                    if value is not None:
                        merged[key] = value
        merged.update(os.environ)
        return merged


CONFIG_DIRECTORY = Path("config") / "local"
TENANT_TEMPLATE_DIRECTORY = Path("tenants") / "tenant.example"


def env_config_dir(*, root: Path | None = None) -> Path:
    """Return config/local, or the directory PLATFORM_ENV_CONFIG_DIR names.

    A deployed task has no repository checkout, so its entrypoint writes the platform's config
    files to PLATFORM_ENV_CONFIG_DIR. An explicit root still selects the checkout layout.
    """
    configured = os.environ.get("PLATFORM_ENV_CONFIG_DIR", "")
    if root is None and configured:
        return Path(configured)
    base = root if root is not None else _REPO_ROOT
    return base / CONFIG_DIRECTORY


def env_config_path(name: str, *, root: Path | None = None, directory: Path | None = None) -> Path:
    """Return the file the config name resolves to.

    A tenant-only file comes only from the tenant's bundle, and a replaceable file from the bundle
    when it holds one and otherwise from the platform directory. An explicit root selects the
    checkout's config/local and no bundle, and an explicit directory reads that directory alone.
    """
    if directory is not None:
        return directory / f"{name}.yaml"
    if root is None:
        from services.integration.tenant_config import (
            TENANT_ONLY_FILES,
            tenant_bundle_dir,
            tenant_file,
        )

        bundled = tenant_file(name)
        if bundled is not None:
            return bundled
        if name in TENANT_ONLY_FILES:
            bundle = tenant_bundle_dir()
            if bundle is None:
                raise ConfigError(
                    f"{name}.yaml is a tenant file, and this process reads no tenant's bundle"
                )
            return bundle / f"{name}.yaml"
    return env_config_dir(root=root) / f"{name}.yaml"


def load_env_config(
    name: str, *, root: Path | None = None, directory: Path | None = None
) -> dict[str, Any]:
    """Load config/local/<name>.yaml, or the tenant bundle's copy, as a mapping."""
    path = env_config_path(name, root=root, directory=directory)
    if not path.exists():
        template = config_template(name).relative_to(env_config_dir())
        raise ConfigError(
            f"config file missing: {path}. Copy {CONFIG_DIRECTORY / template} to create it"
        )
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return parsed


def load_platform_config(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """Load config/platform/<name>.yaml, the tracked configuration every deployment shares."""
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
    name: str, *, root: Path | None, suffix: str, directory: Path | None = None
) -> dict[str, Any] | None:
    path = (
        env_config_path(name, root=root, directory=directory)
        if suffix == ""
        else (directory or env_config_dir(root=root)) / f"{name}.yaml{suffix}"
    )
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


def config_template(name: str, *, root: Path | None = None) -> Path:
    """Return the committed template of the config name.

    A platform file's template sits beside it as <name>.yaml.example. A tenant-only file's sits in
    tenants/tenant.example/, as <name>.yaml when every tenant copies it and as <name>.yaml.example
    when a tenant copies it only to use it. Where the template folder is absent, as in a deployed
    image, the folder itself is returned.
    """
    from services.integration.tenant_config import TENANT_ONLY_FILES

    directory = env_config_dir(root=root)
    if name not in TENANT_ONLY_FILES:
        return directory / f"{name}.yaml.example"
    folder = directory / TENANT_TEMPLATE_DIRECTORY
    for candidate in (folder / f"{name}.yaml", folder / f"{name}.yaml.example"):
        if candidate.is_file():
            return candidate
    return folder


def config_status(
    name: str, *, root: Path | None = None, current: int | None = None
) -> ConfigStatus:
    current_version = current if current is not None else current_config_version(name)
    template = config_template(name, root=root)
    example = _read_config_file(
        name,
        root=root,
        suffix=template.name.removeprefix(f"{name}.yaml"),
        directory=template.parent,
    )
    if example is None:
        template_name = CONFIG_DIRECTORY / template.relative_to(env_config_dir(root=root))
        raise ConfigError(f"missing committed template {template_name}")
    example_version = config_version(example)
    example_matches_current = example_version == current_version
    operator = _read_config_file(name, root=root, suffix="")
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
    name: str,
    *,
    root: Path | None = None,
    directory: Path | None = None,
    current: int | None = None,
) -> None:
    current_version = current if current is not None else current_config_version(name)
    operator = _read_config_file(name, root=root, suffix="", directory=directory)
    if operator is None:
        return
    operator_version = config_version(operator)
    if operator_version >= current_version:
        return
    path = env_config_path(name, root=root, directory=directory)
    raise ConfigError(
        f"{path} is version {operator_version} but this release "
        f"expects version {current_version}. Run `inv config-check` and add the new keys."
    )
