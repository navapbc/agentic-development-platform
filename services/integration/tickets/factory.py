"""Config-driven selection of the ticket backend, defaulting to Jira."""

from __future__ import annotations

from scripts.config import Settings, get_settings

from services.integration.credentials import ConfigError
from services.integration.jira.adapter import (
    build_jira_adapter,
    project_of,
)
from services.integration.jira.credentials import (
    JiraCredentialResolver,
    StatusFlow,
    build_jira_credential_resolver,
)
from services.integration.tickets.backend import JIRA, TICKET_BACKENDS, TicketBackend


def resolve_ticket_backend_name(settings: Settings | None = None) -> str:
    """Return the configured ticket backend name, defaulting to Jira when unset."""
    resolved = settings or get_settings()
    name = (resolved.ticket_backend or JIRA).strip().lower()
    if name not in TICKET_BACKENDS:
        raise ConfigError(
            f"unknown ticket_backend {name!r}; expected one of {', '.join(TICKET_BACKENDS)}"
        )
    return name


def build_ticket_backend(
    settings: Settings | None = None,
    *,
    project: str,
    resolver: JiraCredentialResolver | None = None,
) -> TicketBackend:
    """Return the ticket backend for a project, selected by config.

    Defaults to the Jira adapter so a deployment that configures no ticket
    backend behaves exactly as before.
    """
    resolved = settings or get_settings()
    name = resolve_ticket_backend_name(resolved)
    if name == JIRA:
        return build_jira_adapter(resolved, project=project, resolver=resolver)
    from services.integration.tickets.rebar_backend import build_rebar_ticket_backend

    return build_rebar_ticket_backend(resolved, project=project)


def resolve_status_flow(
    settings: Settings | None = None,
    *,
    project: str,
    resolver: JiraCredentialResolver | None = None,
) -> StatusFlow:
    """Return the lifecycle status flow for a project under the configured backend."""
    resolved = settings or get_settings()
    name = resolve_ticket_backend_name(resolved)
    if name == JIRA:
        active = resolver or build_jira_credential_resolver(resolved)
        return active.status_flow(project)
    from services.integration.tickets.rebar_config import load_rebar_backend_config

    return load_rebar_backend_config(resolved).status_flow


def build_ticket_backend_for_issue(
    issue_key: str,
    settings: Settings | None = None,
    *,
    resolver: JiraCredentialResolver | None = None,
) -> TicketBackend:
    return build_ticket_backend(settings, project=project_of(issue_key), resolver=resolver)


def ticket_projects(settings: Settings | None = None) -> set[str]:
    """Return the allowlisted ticket projects for the configured ticket backend."""
    resolved = settings or get_settings()
    name = resolve_ticket_backend_name(resolved)
    if name == JIRA:
        from services.integration.jira.credentials import allowed_projects

        return set(allowed_projects(resolved))
    from services.integration.tickets.rebar_config import load_rebar_backend_config

    project = load_rebar_backend_config(resolved).project.strip()
    return {project} if project else set()
