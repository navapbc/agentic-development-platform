"""The ticket write-back authorization contract.

Every ticket write-back (linkback, status transition, approval recording) runs
under this contract. It validates ticket ownership against the run, enforces a
least-privilege scope allowlist, and produces an audit record naming the actor,
backend, project, ticket, and run id. Per-project credentials and idempotent
concurrency are supplied by the ticket backend and the side-effect ledger the
caller already holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.integration.credentials import ConfigError

STATUS = "status"
COMMENT = "comment"
APPROVAL = "approval"
LINK = "link"
WRITE_SCOPES = (STATUS, COMMENT, APPROVAL, LINK)

_DEFAULT_ALLOWED_SCOPES = WRITE_SCOPES


class WriteBackError(Exception):
    """Raised when a ticket write-back violates the authorization contract."""


@dataclass(frozen=True)
class WriteBackContext:
    actor: str
    backend: str
    project: str
    issue_key: str
    run_id: str


def _project_of(issue_key: str) -> str:
    return issue_key.split("-", 1)[0] if "-" in issue_key else issue_key


def authorize_write_back(
    ctx: WriteBackContext,
    *,
    scope: str,
    run_owner_issue_key: str,
    allowed_scopes: tuple[str, ...] = _DEFAULT_ALLOWED_SCOPES,
) -> None:
    """Validate ownership and scope, raising WriteBackError on any violation."""
    if scope not in WRITE_SCOPES:
        raise WriteBackError(f"unknown write-back scope {scope!r}")
    if scope not in allowed_scopes:
        raise WriteBackError(f"scope {scope!r} is outside the write-back allowlist")
    if not ctx.issue_key:
        raise WriteBackError("write-back requires a target ticket")
    if ctx.issue_key.strip().lower() != run_owner_issue_key.strip().lower():
        raise WriteBackError(
            f"ticket {ctx.issue_key!r} is not owned by run {ctx.run_id!r} "
            f"(owner {run_owner_issue_key!r})"
        )
    if ctx.project and _project_of(ctx.issue_key) != ctx.project:
        raise WriteBackError(f"ticket {ctx.issue_key!r} does not belong to project {ctx.project!r}")


def write_back_audit(ctx: WriteBackContext, *, scope: str, outcome: str) -> dict[str, Any]:
    return {
        "event": "ticket_write_back",
        "actor": ctx.actor,
        "backend": ctx.backend,
        "project": ctx.project,
        "ticket": ctx.issue_key,
        "run_id": ctx.run_id,
        "scope": scope,
        "outcome": outcome,
    }


def parse_write_back_scopes(raw: Mapping[str, Any]) -> tuple[str, ...]:
    section = raw.get("write_back")
    if section is None:
        return _DEFAULT_ALLOWED_SCOPES
    if not isinstance(section, Mapping):
        raise ConfigError("tickets config section 'write_back' is not a mapping")
    scopes = section.get("allowed_scopes")
    if scopes is None:
        return _DEFAULT_ALLOWED_SCOPES
    if not isinstance(scopes, list | tuple):
        raise ConfigError("tickets write_back.allowed_scopes must be a list")
    parsed: list[str] = []
    for item in scopes:
        name = str(item).strip()
        if name and name not in WRITE_SCOPES:
            raise ConfigError(f"unknown write-back scope {name!r} in allowlist")
        if name:
            parsed.append(name)
    return tuple(parsed)


def load_write_back_scopes(
    settings: Any = None, *, environment: str | None = None, root: Path | None = None
) -> tuple[str, ...]:
    from scripts.config import get_settings

    from services.integration.credentials import load_env_config, require_current_config

    resolved = settings or get_settings()
    env = environment or resolved.environment
    try:
        require_current_config("tickets", environment=env, root=root)
    except ConfigError:
        return _DEFAULT_ALLOWED_SCOPES
    return parse_write_back_scopes(load_env_config("tickets", environment=env, root=root))
