"""Jira credentials resolved from named sites, a default identity, and per-project overrides."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretResolver,
    load_env_config,
)

CLOUD = "cloud"
DATACENTER = "datacenter"
_AUTH_MODES = {CLOUD, DATACENTER}


@dataclass(frozen=True)
class JiraProjectCredential:
    url: str
    auth_mode: str
    email: str
    token: str
    board: str
    project: str


@dataclass(frozen=True)
class _Site:
    url: str
    auth_mode: str


@dataclass(frozen=True)
class _Identity:
    site: str
    email: str | None
    token_ref: str
    board: str


@dataclass(frozen=True)
class _ProjectOverride:
    site: str | None
    email: str | None
    token_ref: str | None
    board: str | None


@dataclass(frozen=True)
class JiraSourceConfig:
    sites: dict[str, _Site]
    default: _Identity | None
    projects: dict[str, _ProjectOverride]


class UnconfiguredProjectError(RuntimeError):
    """Raised when an allowlisted project resolves to no complete site, token, and board."""

    def __init__(self, project: str) -> None:
        super().__init__(project)
        self.project = project

    def __str__(self) -> str:
        return f"no complete Jira source (site, token, board) for project: {self.project}"


def _string_fields(raw: Mapping[str, object], context: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str | int):
            raise ConfigError(f"jira config {context} field {key} is not a scalar")
        fields[str(key)] = str(value)
    return fields


def _parse_sites(raw: Mapping[str, object]) -> dict[str, _Site]:
    sites_raw = raw.get("sites") or {}
    if not isinstance(sites_raw, Mapping):
        raise ConfigError("jira config 'sites' is not a mapping")
    sites: dict[str, _Site] = {}
    for name, fields in sites_raw.items():
        if not isinstance(fields, Mapping):
            raise ConfigError(f"jira config site is not a mapping: {name}")
        parsed = _string_fields(fields, f"site {name}")
        missing = [field for field in ("url", "auth_mode") if not parsed.get(field)]
        if missing:
            raise ConfigError(f"jira site {name} missing fields: {', '.join(missing)}")
        auth_mode = parsed["auth_mode"].strip().lower()
        if auth_mode not in _AUTH_MODES:
            raise ConfigError(f"jira site {name} has unsupported auth_mode: {parsed['auth_mode']}")
        sites[str(name)] = _Site(url=parsed["url"], auth_mode=auth_mode)
    return sites


def _parse_default(raw: Mapping[str, object], sites: dict[str, _Site]) -> _Identity | None:
    default_raw = raw.get("default")
    if default_raw is None:
        return None
    if not isinstance(default_raw, Mapping):
        raise ConfigError("jira config 'default' is not a mapping")
    parsed = _string_fields(default_raw, "default")
    missing = [field for field in ("site", "token_ref", "board") if not parsed.get(field)]
    if missing:
        raise ConfigError(f"jira config default missing fields: {', '.join(missing)}")
    if parsed["site"] not in sites:
        raise ConfigError(f"jira config default references unknown site: {parsed['site']}")
    return _Identity(
        site=parsed["site"],
        email=parsed.get("email"),
        token_ref=parsed["token_ref"],
        board=parsed["board"],
    )


def _parse_projects(
    raw: Mapping[str, object], sites: dict[str, _Site]
) -> dict[str, _ProjectOverride]:
    projects_raw = raw.get("projects") or {}
    if not isinstance(projects_raw, Mapping):
        raise ConfigError("jira config 'projects' is not a mapping")
    projects: dict[str, _ProjectOverride] = {}
    for name, fields in projects_raw.items():
        if not isinstance(fields, Mapping):
            raise ConfigError(f"jira config project is not a mapping: {name}")
        parsed = _string_fields(fields, f"project {name}")
        site = parsed.get("site")
        if site is not None and site not in sites:
            raise ConfigError(f"jira project {name} references unknown site: {site}")
        projects[str(name)] = _ProjectOverride(
            site=site,
            email=parsed.get("email"),
            token_ref=parsed.get("token_ref"),
            board=parsed.get("board"),
        )
    return projects


def parse_jira_config(raw: Mapping[str, object]) -> JiraSourceConfig:
    sites = _parse_sites(raw)
    default = _parse_default(raw, sites)
    projects = _parse_projects(raw, sites)
    return JiraSourceConfig(sites=sites, default=default, projects=projects)


class JiraCredentialResolver:
    def __init__(
        self,
        config: JiraSourceConfig,
        allowlist: set[str],
        secrets: SecretResolver,
    ) -> None:
        self._config = config
        self._allowlist = set(allowlist)
        self._secrets = secrets

    def projects(self) -> set[str]:
        return {project for project in self._allowlist if self._effective(project) is not None}

    def resolve(self, project: str) -> JiraProjectCredential | None:
        if project not in self._allowlist:
            return None
        effective = self._effective(project)
        if effective is None:
            raise UnconfiguredProjectError(project)
        site, email, token_ref, board = effective
        token = self._secrets.resolve(token_ref)
        return JiraProjectCredential(
            url=site.url,
            auth_mode=site.auth_mode,
            email=email or "",
            token=token,
            board=board,
            project=project,
        )

    def _effective(self, project: str) -> tuple[_Site, str | None, str, str] | None:
        override = self._config.projects.get(project)
        default = self._config.default
        site_name = (override.site if override else None) or (default.site if default else None)
        email = (override.email if override else None) or (default.email if default else None)
        token_ref = (override.token_ref if override else None) or (
            default.token_ref if default else None
        )
        board = (override.board if override else None) or (default.board if default else None)
        if not site_name or not token_ref or not board:
            return None
        site = self._config.sites.get(site_name)
        if site is None:
            return None
        if site.auth_mode == CLOUD and not email:
            return None
        if site.auth_mode == DATACENTER:
            email = None
        return site, email, token_ref, board


def build_jira_credential_resolver(
    settings: Settings | None = None,
    *,
    secret_resolver: SecretResolver | None = None,
    environment: str | None = None,
) -> JiraCredentialResolver:
    resolved = settings or get_settings()
    env = environment or resolved.environment
    raw = load_env_config("jira", environment=env)
    config = parse_jira_config(raw)
    secrets = secret_resolver or EnvSecretResolver()
    return JiraCredentialResolver(config, resolved.allowed_projects(), secrets)
