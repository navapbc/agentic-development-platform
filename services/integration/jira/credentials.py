"""Jira credentials resolved from named sites, a default identity, and per-project overrides."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts.config import Settings, get_settings

from services.integration.credentials import (
    ConfigError,
    EnvSecretResolver,
    SecretResolver,
    load_env_config,
    parse_allowlist,
    require_current_config,
)

CLOUD = "cloud"
DATACENTER = "datacenter"
_AUTH_MODES = {CLOUD, DATACENTER}
_FLOW_STAGES = ("work_started", "ready_for_review")


@dataclass(frozen=True)
class StatusFlow:
    work_started: str = ""
    ready_for_review: str = ""


@dataclass(frozen=True)
class JiraProjectCredential:
    url: str
    auth_mode: str
    email: str
    token: str
    board: str
    project: str
    statuses: tuple[str, ...] = ()
    status_flow: StatusFlow = field(default_factory=StatusFlow)
    default_repo: str = ""


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
    statuses: tuple[str, ...] = ()
    flow: StatusFlow = field(default_factory=StatusFlow)
    default_repo: str = ""


@dataclass(frozen=True)
class _ProjectOverride:
    site: str | None
    email: str | None
    token_ref: str | None
    board: str | None
    statuses: tuple[str, ...] | None = None
    flow: StatusFlow | None = None
    default_repo: str | None = None


@dataclass(frozen=True)
class _Effective:
    site: _Site
    email: str | None
    token_ref: str
    board: str
    statuses: tuple[str, ...]
    flow: StatusFlow
    default_repo: str


@dataclass(frozen=True)
class JiraSourceConfig:
    sites: dict[str, _Site]
    default: _Identity | None
    projects: dict[str, _ProjectOverride]
    allowlist: tuple[str, ...] = ()
    acceptance_field: str = ""


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
        if key in ("statuses", "flow"):
            continue
        if value is None:
            continue
        if not isinstance(value, str | int):
            raise ConfigError(f"jira config {context} field {key} is not a scalar")
        fields[str(key)] = str(value)
    return fields


def _parse_statuses(raw: object, context: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise ConfigError(f"jira config {context} 'statuses' is not a list")
    statuses: list[str] = []
    for value in raw:
        if not isinstance(value, str | int):
            raise ConfigError(f"jira config {context} status is not a scalar")
        statuses.append(str(value))
    return tuple(statuses)


def _parse_flow(raw: object, context: str) -> StatusFlow | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"jira config {context} 'flow' is not a mapping")
    unknown = [str(key) for key in raw if str(key) not in _FLOW_STAGES]
    if unknown:
        raise ConfigError(f"jira config {context} flow has unknown stages: {', '.join(unknown)}")
    values: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str | int):
            raise ConfigError(f"jira config {context} flow stage {key} is not a scalar")
        values[str(key)] = str(value)
    return StatusFlow(
        work_started=values.get("work_started", ""),
        ready_for_review=values.get("ready_for_review", ""),
    )


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
        statuses=_parse_statuses(default_raw.get("statuses"), "default") or (),
        flow=_parse_flow(default_raw.get("flow"), "default") or StatusFlow(),
        default_repo=parsed.get("default_repo", ""),
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
            statuses=_parse_statuses(fields.get("statuses"), f"project {name}"),
            flow=_parse_flow(fields.get("flow"), f"project {name}"),
            default_repo=parsed.get("default_repo"),
        )
    return projects


def _parse_acceptance_field(raw: Mapping[str, object]) -> str:
    value = raw.get("acceptance_field") or ""
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise ConfigError("jira config 'acceptance_field' is not a scalar")
    return str(value)


def parse_jira_config(raw: Mapping[str, object]) -> JiraSourceConfig:
    sites = _parse_sites(raw)
    default = _parse_default(raw, sites)
    projects = _parse_projects(raw, sites)
    return JiraSourceConfig(
        sites=sites,
        default=default,
        projects=projects,
        allowlist=parse_allowlist(raw, "jira"),
        acceptance_field=_parse_acceptance_field(raw),
    )


def _validate_flow(flow: StatusFlow, statuses: tuple[str, ...]) -> None:
    if not statuses:
        return
    available = {status.strip().lower() for status in statuses}
    for stage_status in (flow.work_started, flow.ready_for_review):
        if stage_status and stage_status.strip().lower() not in available:
            raise ConfigError(
                f"jira status '{stage_status}' is not in the board's declared statuses"
            )


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
        _validate_flow(effective.flow, effective.statuses)
        token = self._secrets.resolve(effective.token_ref)
        return JiraProjectCredential(
            url=effective.site.url,
            auth_mode=effective.site.auth_mode,
            email=effective.email or "",
            token=token,
            board=effective.board,
            project=project,
            statuses=effective.statuses,
            status_flow=effective.flow,
            default_repo=effective.default_repo,
        )

    def status_flow(self, project: str) -> StatusFlow:
        if project not in self._allowlist:
            return StatusFlow()
        effective = self._effective(project)
        if effective is None:
            return StatusFlow()
        _validate_flow(effective.flow, effective.statuses)
        return effective.flow

    def _effective(self, project: str) -> _Effective | None:
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
        if override is not None and override.statuses is not None:
            statuses = override.statuses
        elif default is not None:
            statuses = default.statuses
        else:
            statuses = ()
        if override is not None and override.flow is not None:
            flow = override.flow
        elif default is not None:
            flow = default.flow
        else:
            flow = StatusFlow()
        default_repo = (override.default_repo if override else None) or (
            default.default_repo if default else ""
        )
        return _Effective(site, email, token_ref, board, statuses, flow, default_repo or "")


def build_jira_credential_resolver(
    settings: Settings | None = None,
    *,
    secret_resolver: SecretResolver | None = None,
    environment: str | None = None,
) -> JiraCredentialResolver:
    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("jira", environment=env)
    raw = load_env_config("jira", environment=env)
    config = parse_jira_config(raw)
    secrets = secret_resolver or EnvSecretResolver()
    resolver = JiraCredentialResolver(config, set(config.allowlist), secrets)
    for project in resolver.projects():
        resolver.status_flow(project)
    return resolver


def allowed_projects(
    settings: Settings | None = None,
    *,
    environment: str | None = None,
    root: Path | None = None,
) -> set[str]:
    """Return the allowlisted projects declared in config/<env>/jira.yaml."""
    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("jira", environment=env, root=root)
    raw = load_env_config("jira", environment=env, root=root)
    return set(parse_jira_config(raw).allowlist)


def acceptance_field(
    settings: Settings | None = None,
    *,
    environment: str | None = None,
    root: Path | None = None,
) -> str:
    """Return the acceptance-criteria field id declared in config/<env>/jira.yaml."""
    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("jira", environment=env, root=root)
    raw = load_env_config("jira", environment=env, root=root)
    return parse_jira_config(raw).acceptance_field


def build_single_project_resolver(
    *,
    url: str,
    auth_mode: str,
    email: str,
    token: str,
    board: str,
    project: str,
    statuses: tuple[str, ...] = (),
    flow: StatusFlow | None = None,
    default_repo: str = "",
) -> JiraCredentialResolver:
    mode = auth_mode.strip().lower()
    if mode not in _AUTH_MODES:
        raise ConfigError(f"unsupported jira auth_mode: {auth_mode}")
    config = JiraSourceConfig(
        sites={"default": _Site(url=url, auth_mode=mode)},
        default=_Identity(
            site="default",
            email=email or None,
            token_ref="__token__",
            board=board,
            statuses=tuple(statuses),
            flow=flow or StatusFlow(),
            default_repo=default_repo,
        ),
        projects={},
    )
    return JiraCredentialResolver(config, {project}, EnvSecretResolver({"__token__": token}))
