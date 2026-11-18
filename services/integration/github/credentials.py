"""GitHub credentials resolved from named hosts, a default identity, and per-repo overrides.

github.yaml names the hosts and the default identity. Which repositories are reachable, and
any per-repository host, owner, or token override, come from repositories.yaml.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretResolver,
    load_env_config,
    require_current_config,
)
from services.integration.github.git_auth import GitAuth
from services.integration.repositories import GITHUB, RepositoryRegistry, load_repository_registry


@dataclass(frozen=True)
class GitHubRepoCredential:
    api_url: str
    owner: str
    git_base_url: str
    token: str


@dataclass(frozen=True)
class _Host:
    api_url: str
    git_base_url: str


@dataclass(frozen=True)
class _Identity:
    host: str
    owner: str
    token_ref: str


@dataclass(frozen=True)
class _RepoOverride:
    host: str | None
    owner: str | None
    token_ref: str | None


@dataclass(frozen=True)
class GitHubSourceConfig:
    hosts: dict[str, _Host]
    default: _Identity | None
    repositories: dict[str, _RepoOverride]


class UnconfiguredRepositoryError(RuntimeError):
    """Raised when an allowlisted repository resolves to no complete host, owner, and token."""

    def __init__(self, repo: str) -> None:
        super().__init__(repo)
        self.repo = repo

    def __str__(self) -> str:
        return f"no complete GitHub source (host, owner, token) for repository: {self.repo}"


def _string_fields(raw: Mapping[str, object], context: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str | int):
            raise ConfigError(f"github config {context} field {key} is not a scalar")
        fields[str(key)] = str(value)
    return fields


def _parse_hosts(raw: Mapping[str, object]) -> dict[str, _Host]:
    hosts_raw = raw.get("hosts") or {}
    if not isinstance(hosts_raw, Mapping):
        raise ConfigError("github config 'hosts' is not a mapping")
    hosts: dict[str, _Host] = {}
    for name, fields in hosts_raw.items():
        if not isinstance(fields, Mapping):
            raise ConfigError(f"github config host is not a mapping: {name}")
        parsed = _string_fields(fields, f"host {name}")
        missing = [field for field in ("api_url", "git_base_url") if not parsed.get(field)]
        if missing:
            raise ConfigError(f"github host {name} missing fields: {', '.join(missing)}")
        hosts[str(name)] = _Host(api_url=parsed["api_url"], git_base_url=parsed["git_base_url"])
    return hosts


def _parse_default(raw: Mapping[str, object], hosts: dict[str, _Host]) -> _Identity | None:
    default_raw = raw.get("default")
    if default_raw is None:
        return None
    if not isinstance(default_raw, Mapping):
        raise ConfigError("github config 'default' is not a mapping")
    parsed = _string_fields(default_raw, "default")
    missing = [field for field in ("host", "owner", "token_ref") if not parsed.get(field)]
    if missing:
        raise ConfigError(f"github config default missing fields: {', '.join(missing)}")
    if parsed["host"] not in hosts:
        raise ConfigError(f"github config default references unknown host: {parsed['host']}")
    return _Identity(host=parsed["host"], owner=parsed["owner"], token_ref=parsed["token_ref"])


_MOVED_SECTIONS = ("allowlist", "repositories")


def parse_github_config(
    raw: Mapping[str, object], registry: RepositoryRegistry | None = None
) -> GitHubSourceConfig:
    """Parse github.yaml, taking each GitHub repository's overrides from the registry."""
    moved = [section for section in _MOVED_SECTIONS if section in raw]
    if moved:
        raise ConfigError(
            f"github.yaml sets {' and '.join(moved)}, which repositories.yaml now holds. "
            "List each repository in config/<env>/repositories.yaml with its host, owner, or "
            "token_ref override, and remove the section from github.yaml."
        )
    hosts = _parse_hosts(raw)
    default = _parse_default(raw, hosts)
    repositories: dict[str, _RepoOverride] = {}
    for name in sorted(registry.names(GITHUB)) if registry is not None else ():
        repository = registry.require(name)
        if repository.host is not None and repository.host not in hosts:
            raise ConfigError(
                f"repositories.yaml repository {name} references a host github.yaml does not "
                f"define: {repository.host}"
            )
        repositories[name] = _RepoOverride(
            host=repository.host, owner=repository.owner, token_ref=repository.token_ref
        )
    return GitHubSourceConfig(hosts=hosts, default=default, repositories=repositories)


class GitHubCredentialResolver:
    def __init__(
        self,
        config: GitHubSourceConfig,
        allowlist: set[str],
        secrets: SecretResolver,
    ) -> None:
        self._config = config
        self._allowlist = set(allowlist)
        self._secrets = secrets

    def repos(self) -> set[str]:
        return {repo for repo in self._allowlist if self._effective(repo) is not None}

    def resolve(self, repo: str) -> GitHubRepoCredential | None:
        if repo not in self._allowlist:
            return None
        effective = self._effective(repo)
        if effective is None:
            raise UnconfiguredRepositoryError(repo)
        host, owner, token_ref = effective
        token = self._secrets.resolve(token_ref)
        return GitHubRepoCredential(
            api_url=host.api_url,
            owner=owner,
            git_base_url=host.git_base_url,
            token=token,
        )

    def git_remote_url(self, repo: str) -> str:
        """Return the remote URL for `repo`, carrying no credential.

        Git writes a clone URL verbatim into `.git/config`, so a token embedded here would
        persist in every workspace the platform materializes. Callers pair this with
        `git_auth` instead.
        """
        cred = self.resolve(repo)
        if cred is None:
            raise UnconfiguredRepositoryError(repo)
        base = cred.git_base_url.rstrip("/")
        return f"{base}/{cred.owner}/{repo}.git"

    def git_auth(self, repo: str) -> GitAuth:
        cred = self.resolve(repo)
        if cred is None:
            raise UnconfiguredRepositoryError(repo)
        return GitAuth(cred.token, cred.git_base_url)

    def _effective(self, repo: str) -> tuple[_Host, str, str] | None:
        override = self._config.repositories.get(repo)
        default = self._config.default
        host_name = (override.host if override else None) or (default.host if default else None)
        owner = (override.owner if override else None) or (default.owner if default else None)
        token_ref = (override.token_ref if override else None) or (
            default.token_ref if default else None
        )
        if not host_name or not owner or not token_ref:
            return None
        host = self._config.hosts.get(host_name)
        if host is None:
            return None
        return host, owner, token_ref


def build_github_credential_resolver(
    settings: Settings | None = None,
    *,
    secret_resolver: SecretResolver | None = None,
    environment: str | None = None,
) -> GitHubCredentialResolver:
    resolved = settings or get_settings()
    env = environment or resolved.environment
    registry = load_repository_registry(resolved, environment=env)
    require_current_config("github", environment=env)
    raw = load_env_config("github", environment=env)
    config = parse_github_config(raw, registry)
    secrets = secret_resolver or EnvSecretResolver()
    return GitHubCredentialResolver(config, registry.names(GITHUB), secrets)


def build_single_identity_resolver(
    *,
    api_url: str,
    owner: str,
    git_base_url: str,
    token: str,
    repos: set[str],
) -> GitHubCredentialResolver:
    """Build a resolver for one host and owner, which resolves to no credential without a token.

    The token reference is synthetic, so an empty token would otherwise resolve to a secret
    named `__token__` that no operator can set, and the resolver would raise where it means
    that this caller configured no credential.
    """
    identity = _Identity(host="default", owner=owner, token_ref="__token__") if token else None
    config = GitHubSourceConfig(
        hosts={"default": _Host(api_url=api_url, git_base_url=git_base_url)},
        default=identity,
        repositories={},
    )
    return GitHubCredentialResolver(config, set(repos), EnvSecretResolver({"__token__": token}))
