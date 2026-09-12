"""Vendor-neutral ticket-backend contract and factory."""

from __future__ import annotations

from services.integration.tickets.backend import (
    JIRA,
    REBAR,
    TICKET_BACKENDS,
    DependencyBackend,
    TicketBackend,
)
from services.integration.tickets.factory import (
    build_ticket_backend,
    build_ticket_backend_for_issue,
    resolve_status_flow,
    resolve_ticket_backend_name,
)

__all__ = [
    "JIRA",
    "REBAR",
    "TICKET_BACKENDS",
    "DependencyBackend",
    "TicketBackend",
    "build_ticket_backend",
    "build_ticket_backend_for_issue",
    "resolve_status_flow",
    "resolve_ticket_backend_name",
]
