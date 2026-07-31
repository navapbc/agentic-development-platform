"""Tests for the idempotent per-instance Jira PR linkback comment."""

from __future__ import annotations

import json

from services.integration.jira.linkback import post_pr_linkback
from services.integration.jira.render import CLOUD, DATACENTER
from services.integration.jira.writes import APPLIED, SKIPPED, comment_marker


class _FakeAdapter:
    def __init__(self, source: str) -> None:
        self.source = source
        self.comments: list[str] = []

    def list_comment_bodies(self, issue_key: str):
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str):
        self.comments.append(body)
        return {"id": len(self.comments)}


def test_datacenter_linkback_renders_wiki_and_is_idempotent():
    adapter = _FakeAdapter(DATACENTER)

    first = post_pr_linkback(
        adapter,
        "AGENTDEV-1",
        execution_id="exec-1",
        pr_url="https://ghes/acme/svc/pull/7",
        pr_title="Draft PR #7",
        repo="acme/svc",
        branch="agent/AGENTDEV-1/exec-1",
    )
    second = post_pr_linkback(
        adapter,
        "AGENTDEV-1",
        execution_id="exec-1",
        pr_url="https://ghes/acme/svc/pull/7",
        pr_title="Draft PR #7",
        repo="acme/svc",
        branch="agent/AGENTDEV-1/exec-1",
    )

    assert first.action == APPLIED
    assert second.action == SKIPPED
    assert len(adapter.comments) == 1
    body = adapter.comments[0]
    assert "[Draft PR #7|https://ghes/acme/svc/pull/7]" in body
    assert comment_marker("exec-1", "pr_link") in body


def test_cloud_linkback_renders_adf_json_with_marker():
    adapter = _FakeAdapter(CLOUD)

    result = post_pr_linkback(
        adapter,
        "AGENTDEV-2",
        execution_id="exec-9",
        pr_url="https://ghes/acme/svc/pull/9",
        pr_title="Draft PR #9",
        repo="acme/svc",
        branch="agent/AGENTDEV-2/exec-9",
    )

    assert result.action == APPLIED
    body = adapter.comments[0]
    document = json.loads(body)
    assert document["type"] == "doc"
    assert comment_marker("exec-9", "pr_link") in body
    assert "https://ghes/acme/svc/pull/9" in body


def test_cloud_linkback_dedupes_when_marker_present_in_stored_body():
    adapter = _FakeAdapter(CLOUD)
    adapter.comments.append(f"prior comment {comment_marker('exec-9', 'pr_link')}")

    result = post_pr_linkback(
        adapter,
        "AGENTDEV-2",
        execution_id="exec-9",
        pr_url="https://ghes/acme/svc/pull/9",
        pr_title="Draft PR #9",
        repo="acme/svc",
        branch="agent/AGENTDEV-2/exec-9",
    )

    assert result.action == SKIPPED
    assert len(adapter.comments) == 1
