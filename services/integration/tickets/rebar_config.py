"""Operational config for the rebar ticket backend: store location and status flow."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.integration.credentials import ConfigError
from services.integration.jira.credentials import StatusFlow

_FLOW_STAGES = ("work_started", "ready_for_review", "terminal")


@dataclass(frozen=True)
class RebarBackendConfig:
    repo_root: str
    status_flow: StatusFlow = field(default_factory=StatusFlow)
    project: str = ""
    default_repo: str = ""


@dataclass(frozen=True)
class PollConfig:
    reader_roles: tuple[str, ...] = ()
    allowed_fields: tuple[str, ...] = ()
    identity_roles: tuple[tuple[str, str], ...] = ()

    def role_for(self, identity: str) -> str:
        for name, role in self.identity_roles:
            if name == identity:
                return role
        return ""


def _parse_status_flow(raw: Any) -> StatusFlow:
    if raw is None:
        return StatusFlow()
    if not isinstance(raw, Mapping):
        raise ConfigError("tickets backend status_flow is not a mapping")
    unknown = [str(key) for key in raw if str(key) not in _FLOW_STAGES]
    if unknown:
        raise ConfigError(f"tickets backend status_flow has unknown stages: {', '.join(unknown)}")
    values: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str | int):
            raise ConfigError(f"tickets backend status_flow stage {key} is not a scalar")
        values[str(key)] = str(value)
    return StatusFlow(
        work_started=values.get("work_started", ""),
        ready_for_review=values.get("ready_for_review", ""),
        terminal=values.get("terminal", ""),
    )


def parse_rebar_backend_config(raw: Mapping[str, Any]) -> RebarBackendConfig:
    backend = raw.get("backend")
    if not isinstance(backend, Mapping):
        raise ConfigError("tickets config section 'backend' is missing or not a mapping")
    repo_root = backend.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root.strip():
        raise ConfigError("tickets backend.repo_root is required")
    if not Path(repo_root.strip()).is_absolute():
        raise ConfigError(
            "tickets backend.repo_root must be an absolute path and ~ is not expanded, because "
            "operator commands resolve it on the host while the intake poll resolves it inside "
            f"the worker container, so {repo_root.strip()!r} names a different store in each"
        )
    return RebarBackendConfig(
        repo_root=repo_root.strip(),
        status_flow=_parse_status_flow(backend.get("status_flow")),
        project=str(backend.get("project", "")).strip(),
        default_repo=str(backend.get("default_repo", "")).strip(),
    )


def _str_tuple(raw: Any, context: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        raise ConfigError(f"tickets {context} must be a list")
    return tuple(str(item).strip() for item in raw if str(item).strip())


def parse_poll_config(raw: Mapping[str, Any]) -> PollConfig:
    poll = raw.get("poll")
    if poll is None:
        return PollConfig()
    if not isinstance(poll, Mapping):
        raise ConfigError("tickets config section 'poll' is not a mapping")
    identity_roles_raw = poll.get("identity_roles") or {}
    if not isinstance(identity_roles_raw, Mapping):
        raise ConfigError("tickets poll.identity_roles must be a mapping")
    identity_roles = tuple((str(name), str(role)) for name, role in identity_roles_raw.items())
    return PollConfig(
        reader_roles=_str_tuple(poll.get("reader_roles"), "poll.reader_roles"),
        allowed_fields=_str_tuple(poll.get("allowed_fields"), "poll.allowed_fields"),
        identity_roles=identity_roles,
    )


def load_poll_config(
    settings: Any = None, *, environment: str | None = None, root: Path | None = None
) -> PollConfig:
    from scripts.config import get_settings

    from services.integration.credentials import load_env_config, require_current_config

    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("tickets", environment=env, root=root)
    return parse_poll_config(load_env_config("tickets", environment=env, root=root))


def load_rebar_backend_config(
    settings: Any = None, *, environment: str | None = None, root: Path | None = None
) -> RebarBackendConfig:
    from scripts.config import get_settings

    from services.integration.credentials import load_env_config, require_current_config

    resolved = settings or get_settings()
    env = environment or resolved.environment
    require_current_config("tickets", environment=env, root=root)
    return parse_rebar_backend_config(load_env_config("tickets", environment=env, root=root))
