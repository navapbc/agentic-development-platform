"""The source-control repositories an environment works on, from config/<env>/repositories.yaml.

Each repository names its source-control backend, its default branch, and whether repository
intelligence indexes it. A GitHub repository may override the host, owner, or token reference
of the default identity in github.yaml. A local repository names the checkout on the host that
the platform reads and pushes branches into. Inside a container, every repository's checkout
appears at ``<REPOSITORY_ROOT>/<name>``. On the host, a repository with a path is located at
that path, so operator commands work on the same checkouts the containers mount.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.config import Settings, get_settings, in_platform_image

from services.integration.credentials import (
    ConfigError,
    load_env_config,
    require_current_config,
)

GITHUB = "github"
LOCAL = "local"
BACKENDS = (GITHUB, LOCAL)
CONFIG_NAME = "repositories"

_PLATFORM_ROOT = Path(__file__).resolve().parents[2]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GITHUB_FIELDS = ("host", "owner", "token_ref")
_FIELDS = frozenset({"backend", "default_branch", "index", "index_ref", "path", *_GITHUB_FIELDS})
_DEFAULT_FIELDS = frozenset({"backend", "default_branch", "index"})


class UnknownRepositoryError(ConfigError):
    """Raised when a repository is not configured in repositories.yaml."""

    def __init__(self, repo: str) -> None:
        super().__init__(f"repository {repo!r} is not configured in repositories.yaml")
        self.repo = repo


@dataclass(frozen=True)
class Repository:
    name: str
    backend: str
    default_branch: str
    index: bool = False
    index_ref: str | None = None
    path: str | None = None
    host: str | None = None
    owner: str | None = None
    token_ref: str | None = None


@dataclass(frozen=True)
class RepositoryRegistry:
    repositories: Mapping[str, Repository]
    root: str
    host_base: Path | None = None

    def names(self, backend: str | None = None) -> set[str]:
        return {
            name
            for name, repository in self.repositories.items()
            if backend is None or repository.backend == backend
        }

    def get(self, name: str) -> Repository | None:
        return self.repositories.get(name)

    def require(self, name: str) -> Repository:
        repository = self.repositories.get(name)
        if repository is None:
            raise UnknownRepositoryError(name)
        return repository

    def backend_for(self, name: str) -> str:
        return self.require(name).backend

    def location(self, name: str) -> str:
        """Return where this process finds the repository's checkout.

        Inside a container that is ``<root>/<name>``. With a host base, a repository that
        names a path is found at that path, resolved from the host base.
        """
        self.require(name)
        if self.host_base is not None:
            host = self.host_path(name, self.host_base)
            if host is not None:
                return str(host)
        return f"{self.root.rstrip('/')}/{name}"

    def indexed(self) -> dict[str, tuple[str, str | None]]:
        """Return each indexed repository's checkout location and pinned ref."""
        return {
            name: (self.location(name), repository.index_ref)
            for name, repository in sorted(self.repositories.items())
            if repository.index
        }

    def host_path(self, name: str, base: Path) -> Path | None:
        """Return the repository's checkout on the host, resolving a relative path from base."""
        raw = self.require(name).path
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (base / path).resolve()


def _text(value: object, field: str, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise ConfigError(f"repositories.yaml {context} field {field} is not a string")
    text = str(value).strip()
    return text or None


def _flag(value: object, field: str, context: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"repositories.yaml {context} field {field} is not true or false")
    return value


def _backend(value: object, context: str) -> str | None:
    text = _text(value, "backend", context)
    if text is None:
        return None
    backend = text.lower()
    if backend not in BACKENDS:
        raise ConfigError(
            f"repositories.yaml {context} backend {text!r} is not one of {', '.join(BACKENDS)}"
        )
    return backend


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"repositories.yaml {field!r} is not a mapping")
    return value


def _unknown(fields: Mapping[str, Any], allowed: frozenset[str], context: str) -> None:
    unknown = sorted(str(key) for key in fields if key not in allowed)
    if unknown:
        raise ConfigError(f"repositories.yaml {context} has unknown fields: {', '.join(unknown)}")


def parse_repositories(
    raw: Mapping[str, Any], *, root: str, host_base: Path | None = None
) -> RepositoryRegistry:
    defaults = _mapping(raw.get("defaults"), "defaults")
    _unknown(defaults, _DEFAULT_FIELDS, "defaults")
    default_backend = _backend(defaults.get("backend"), "defaults") or GITHUB
    default_branch = _text(defaults.get("default_branch"), "default_branch", "defaults") or "main"
    default_index = _flag(defaults.get("index"), "index", "defaults") or False

    repositories: dict[str, Repository] = {}
    for name, fields in _mapping(raw.get("repositories"), "repositories").items():
        key = str(name)
        if not _NAME.match(key):
            raise ConfigError(f"repositories.yaml repository name {key!r} is not a valid name")
        context = f"repository {key}"
        entry = _mapping(fields, f"repositories.{key}")
        _unknown(entry, _FIELDS, context)
        backend = _backend(entry.get("backend"), context) or default_backend
        github = {field: _text(entry.get(field), field, context) for field in _GITHUB_FIELDS}
        path = _text(entry.get("path"), "path", context)
        if backend == LOCAL:
            stray = [field for field, value in github.items() if value]
            if stray:
                raise ConfigError(
                    f"repositories.yaml {context} uses the local backend and sets "
                    f"{', '.join(stray)}, which only a GitHub repository takes"
                )
            if not path:
                raise ConfigError(
                    f"repositories.yaml {context} uses the local backend and names no path"
                )
        index = _flag(entry.get("index"), "index", context)
        repositories[key] = Repository(
            name=key,
            backend=backend,
            default_branch=_text(entry.get("default_branch"), "default_branch", context)
            or default_branch,
            index=default_index if index is None else index,
            index_ref=_text(entry.get("index_ref"), "index_ref", context),
            path=path,
            **github,
        )
    return RepositoryRegistry(repositories=repositories, root=root, host_base=host_base)


def load_repository_registry(
    settings: Settings | None = None,
    *,
    environment: str | None = None,
    root: Path | None = None,
) -> RepositoryRegistry:
    """Load config/<env>/repositories.yaml, the repositories this environment works on.

    Outside the platform image the registry locates each repository at its host path.
    """
    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config(CONFIG_NAME, environment=env, root=root)
    raw = load_env_config(CONFIG_NAME, environment=env, root=root)
    host_base = None if in_platform_image() else _PLATFORM_ROOT
    return parse_repositories(raw, root=resolved.repository_root, host_base=host_base)
