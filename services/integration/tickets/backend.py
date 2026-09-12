"""The vendor-neutral ticket-backend contract.

Every orchestration call site that reads or writes a ticket depends on this
Protocol rather than on a single vendor adapter. The Jira adapter is the default
implementation and conforms unchanged. A backend also owns how a comment
document renders for its store, so Jira keeps ADF and wiki rendering while a
plain-text store receives plain text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from services.integration.jira.render import Block

JIRA = "jira"
REBAR = "rebar"
TICKET_BACKENDS = (JIRA, REBAR)


@runtime_checkable
class TicketBackend(Protocol):
    """The ticket operations the orchestration layer depends on."""

    def get_issue(self, issue_key: str) -> dict[str, Any]: ...

    def recheck_payload(
        self, issue_key: str, *, raw_comments: Sequence[Any] = ()
    ) -> dict[str, Any]: ...

    def get_status(self, issue_key: str) -> str: ...

    def transition(self, issue_key: str, target_status: str) -> None: ...

    def add_comment(self, issue_key: str, body: str) -> Any: ...

    def list_comment_bodies(self, issue_key: str) -> Sequence[str]: ...

    def search_issues(self, jql: str, *, page_size: int = 50) -> list[dict[str, Any]]: ...

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]: ...

    def render_comment(self, document: list[Block]) -> str: ...


@runtime_checkable
class DependencyBackend(Protocol):
    """Optional cross-ticket dependency graph a ticket backend may support.

    A backend that can link two tickets, such as the rebar store or a Jira
    deployment with a configured dependency link type, implements this so the
    orchestration layer can record and read undeclared dependencies. The
    per-run file touch set is platform run-state and lives in the database
    rather than here. ``dependency_graph_active`` reports whether the graph is
    usable, which rebar answers unconditionally and Jira answers from its
    configured link type, so a backend without a usable graph records nothing
    and pauses on nothing.
    """

    def link(self, from_issue: str, to_issue: str, *, relationship: str) -> None: ...

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]: ...

    def dependents(self, issue_key: str) -> tuple[str, ...]: ...

    def dependency_graph_active(self) -> bool: ...
