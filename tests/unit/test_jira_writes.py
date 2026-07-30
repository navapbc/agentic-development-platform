"""Idempotent write tests: transitions skip in-state, comments dedupe by execution marker."""

from __future__ import annotations

from services.integration.jira.writes import (
    APPLIED,
    SKIPPED,
    IdempotentJiraWriter,
    comment_marker,
)


class _FakeWriteClient:
    def __init__(self, status: str = "To Do", comments: list[str] | None = None) -> None:
        self.status = status
        self.comments: list[str] = list(comments or [])
        self.transitions: list[tuple[str, str]] = []

    def get_status(self, issue_key: str) -> str:
        return self.status

    def transition(self, issue_key: str, target_status: str) -> None:
        self.transitions.append((issue_key, target_status))
        self.status = target_status

    def list_comment_bodies(self, issue_key: str):
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str) -> None:
        self.comments.append(body)


def test_ensure_status_skips_when_already_in_target():
    client = _FakeWriteClient(status="In Progress")
    writer = IdempotentJiraWriter(client)

    result = writer.ensure_status("AGENTDEV-1", "in progress")

    assert result.action == SKIPPED
    assert client.transitions == []


def test_ensure_status_transitions_when_state_differs():
    client = _FakeWriteClient(status="To Do")
    writer = IdempotentJiraWriter(client)

    result = writer.ensure_status("AGENTDEV-1", "In Progress")

    assert result.action == APPLIED
    assert client.transitions == [("AGENTDEV-1", "In Progress")]
    assert client.status == "In Progress"


def test_post_comment_is_idempotent_per_execution_and_kind():
    client = _FakeWriteClient()
    writer = IdempotentJiraWriter(client)

    first = writer.post_comment("AGENTDEV-1", "PR ready", execution_id="e1", kind="pr_link")
    second = writer.post_comment("AGENTDEV-1", "PR ready", execution_id="e1", kind="pr_link")

    assert first.action == APPLIED
    assert second.action == SKIPPED
    assert len(client.comments) == 1
    assert comment_marker("e1", "pr_link") in client.comments[0]


def test_post_comment_distinguishes_kinds():
    client = _FakeWriteClient()
    writer = IdempotentJiraWriter(client)

    writer.post_comment("AGENTDEV-1", "PR ready", execution_id="e1", kind="pr_link")
    result = writer.post_comment("AGENTDEV-1", "All checks pass", execution_id="e1", kind="summary")

    assert result.action == APPLIED
    assert len(client.comments) == 2
