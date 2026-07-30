"""Idempotent Jira transitions and comments over a vendor-neutral write client."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

APPLIED = "applied"
SKIPPED = "skipped"

_MARKER_PREFIX = "rebar-exec"


class JiraWriteClient(Protocol):
    def get_status(self, issue_key: str) -> str: ...

    def transition(self, issue_key: str, target_status: str) -> Any: ...

    def list_comment_bodies(self, issue_key: str) -> Sequence[str]: ...

    def add_comment(self, issue_key: str, body: str) -> Any: ...


@dataclass(frozen=True)
class WriteResult:
    action: str
    detail: str


def comment_marker(execution_id: str, kind: str) -> str:
    return f"[{_MARKER_PREFIX}:{execution_id}:{kind}]"


@dataclass(frozen=True)
class IdempotentJiraWriter:
    client: JiraWriteClient

    def ensure_status(self, issue_key: str, target_status: str) -> WriteResult:
        current = self.client.get_status(issue_key)
        if current.strip().lower() == target_status.strip().lower():
            return WriteResult(SKIPPED, f"already in {target_status!r}")
        self.client.transition(issue_key, target_status)
        return WriteResult(APPLIED, f"transitioned to {target_status!r}")

    def post_comment(
        self, issue_key: str, body: str, *, execution_id: str, kind: str = "note"
    ) -> WriteResult:
        marker = comment_marker(execution_id, kind)
        if any(marker in existing for existing in self.client.list_comment_bodies(issue_key)):
            return WriteResult(SKIPPED, f"comment {kind!r} already present")
        self.client.add_comment(issue_key, f"{body}\n\n{marker}")
        return WriteResult(APPLIED, f"comment {kind!r} posted")
