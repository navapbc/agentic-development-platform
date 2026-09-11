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
