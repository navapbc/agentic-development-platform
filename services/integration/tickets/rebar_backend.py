"""The rebar ticket backend over rebar's in-process Python API.

Conforms to the TicketBackend contract. Reads and writes an already-seeded
git-backed rebar store at ``repo_root``. The S3 sync is rebar's own remote and
is not driven from here. Logical lifecycle stages resolve to the store's own
statuses through the configured status flow.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.integration.jira.render import (
    BulletList,
    Code,
    Heading,
    Link,
    Paragraph,
    Text,
)
from services.integration.tickets.rebar_config import load_rebar_backend_config

if TYPE_CHECKING:
    from scripts.config import Settings

    from services.integration.jira.credentials import StatusFlow
    from services.integration.jira.render import Block, Inline


_REPAIRED_STORES: set[str] = set()


def _repair_store_worktrees(repo_root: str) -> None:
    """Repair git worktree links so a store written from a different filesystem mount
    than the one it was created in resolves. rebar commits ticket writes through a
    linked worktree whose gitdir records an absolute path, which breaks when the store
    is seeded on the host and used in a container. Runs once per store per process.
    """
    if repo_root in _REPAIRED_STORES:
        return
    root = Path(repo_root)
    if root.is_dir():
        for child in root.iterdir():
            if (child / ".git").is_file():
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "repair", str(child)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
    _REPAIRED_STORES.add(repo_root)


def _render_inline(span: Inline) -> str:
    if isinstance(span, Text):
        return span.value
    if isinstance(span, Link):
        return f"{span.text} ({span.url})"
    return ""


def render_blocks_to_text(document: Sequence[Block]) -> str:
    """Render the block vocabulary to plain text for a rebar store comment."""
    lines: list[str] = []
    for block in document:
        if isinstance(block, Heading):
            lines.append(block.text)
        elif isinstance(block, Paragraph):
            lines.append("".join(_render_inline(span) for span in block.spans))
        elif isinstance(block, BulletList):
            for item in block.items:
                lines.append(f"- {''.join(_render_inline(span) for span in item)}")
        elif isinstance(block, Code):
            lines.append(block.text)
    return "\n".join(lines)


def rebar_stored_payload(ticket: dict[str, Any], *, project: str) -> dict[str, Any]:
    """Build the ticket-comparison payload for a rebar ticket.

    Intake seeding and the pre-PR recheck both normalize a rebar ticket through
    this one shape so their identity fields compare equal across the run.
    """
    repos = ticket.get("repos") or []
    return {
        "task_id": str(ticket.get("ticket_id") or ""),
        "project": project,
        "summary": str(ticket.get("title") or ""),
        "description": str(ticket.get("description") or ""),
        "issue_type": str(ticket.get("ticket_type") or ""),
        "status": str(ticket.get("status") or ""),
        "priority": str(ticket.get("priority") or ""),
        "labels": list(ticket.get("tags") or []),
        "acceptance_criteria": "",
        "comments": [str(c.get("body") or "") for c in (ticket.get("comments") or [])],
        "components": [str(name) for name in repos if name],
    }


@dataclass(frozen=True)
class RebarTicketBackend:
    repo_root: str
    status_flow: StatusFlow
    project: str = ""
    source: str = "rebar"

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        import rebar

        return dict(rebar.show_ticket(issue_key, repo_root=self.repo_root))

    def recheck_payload(
        self, issue_key: str, *, raw_comments: Sequence[Any] = ()
    ) -> dict[str, Any]:
        return rebar_stored_payload(self.get_issue(issue_key), project=self.project)

    def get_status(self, issue_key: str) -> str:
        return str(self.get_issue(issue_key).get("status") or "")

    def transition(self, issue_key: str, target_status: str) -> None:
        import rebar

        current = self.get_status(issue_key)
        if current.strip().lower() == target_status.strip().lower():
            return
        _repair_store_worktrees(self.repo_root)
        try:
            rebar.transition(
                issue_key,
                current,
                target_status,
                force=True,
                reason="agent-platform lifecycle",
                repo_root=self.repo_root,
            )
        except rebar.ConcurrencyError:
            if self.get_status(issue_key).strip().lower() == target_status.strip().lower():
                return
            raise

    def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        import rebar

        _repair_store_worktrees(self.repo_root)
        rebar.comment(issue_key, body, repo_root=self.repo_root)
        return {"issue_key": issue_key}

    def list_comment_bodies(self, issue_key: str) -> tuple[str, ...]:
        comments = self.get_issue(issue_key).get("comments") or []
        return tuple(str(item.get("body") or "") for item in comments)

    def search_issues(self, jql: str, *, page_size: int = 50) -> list[dict[str, Any]]:
        import rebar

        return [dict(ticket) for ticket in rebar.list_tickets(repo_root=self.repo_root)]

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        import rebar

        _repair_store_worktrees(self.repo_root)
        ticket_type = str(ticket_data.get("issue_type") or ticket_data.get("ticket_type") or "task")
        title = str(ticket_data.get("summary") or ticket_data.get("title") or "")
        description = ticket_data.get("description")
        priority = ticket_data.get("priority")
        ticket_id = rebar.create_ticket(
            ticket_type,
            title,
            description=str(description) if description is not None else None,
            priority=int(priority) if isinstance(priority, int) else None,
            repo_root=self.repo_root,
        )
        return self.get_issue(str(ticket_id))

    def render_comment(self, document: list[Block]) -> str:
        return render_blocks_to_text(document)

    def link(self, from_issue: str, to_issue: str, *, relationship: str = "depends_on") -> None:
        import rebar

        _repair_store_worktrees(self.repo_root)
        existing = {dep.get("target_id") for dep in self._deps(from_issue).get("deps", [])}
        if to_issue in existing:
            return
        rebar.link(from_issue, to_issue, relationship, repo_root=self.repo_root)

    def dependency_graph_active(self) -> bool:
        return True

    def declared_dependencies(self, issue_key: str) -> tuple[str, ...]:
        return tuple(str(dep) for dep in self._deps(issue_key).get("blockers", []))

    def dependents(self, issue_key: str) -> tuple[str, ...]:
        import rebar

        ticket: dict[str, Any] = dict(
            rebar.show_ticket(issue_key, repo_root=self.repo_root, include_inbound=True)
        )
        inbound = ticket.get("inbound_deps") or []
        return tuple(
            str(entry.get("from_id"))
            for entry in inbound
            if entry.get("from_id") and entry.get("relation") == "depends_on"
        )

    def _deps(self, issue_key: str) -> dict[str, Any]:
        import rebar

        return dict(rebar.deps(issue_key, repo_root=self.repo_root))


def build_rebar_ticket_backend(
    settings: Settings | None = None, *, project: str = ""
) -> RebarTicketBackend:
    config = load_rebar_backend_config(settings)
    return RebarTicketBackend(
        repo_root=config.repo_root,
        status_flow=config.status_flow,
        project=config.project,
    )
