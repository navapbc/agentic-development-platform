"""Tests for the idempotent per-instance Jira PR linkback comment."""

from __future__ import annotations

import json

from services.integration.jira.linkback import post_pr_linkback, render_linkback
from services.integration.jira.render import CLOUD, DATACENTER
from services.integration.jira.writes import APPLIED, SKIPPED


class _FakeAdapter:
    def __init__(self, source: str) -> None:
        self.source = source
        self.comments: list[str] = []

    def list_comment_bodies(self, issue_key: str):
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str):
        self.comments.append(body)
        return {"id": len(self.comments)}

    def render_comment(self, document):
        return render_linkback(document, source=self.source)


def _adf_visible_text(node) -> str:
    if isinstance(node, dict):
        parts = [node["text"]] if node.get("type") == "text" else []
        parts.extend(_adf_visible_text(child) for child in node.get("content", ()))
        return "".join(parts)
    return ""


class _CloudNormalizingAdapter:
    """Mirrors the live cloud adapter: read-back yields visible text only, no href."""

    def __init__(self) -> None:
        self.source = CLOUD
        self._raw: list[str] = []

    def list_comment_bodies(self, issue_key: str):
        return tuple(_adf_visible_text(json.loads(body)) for body in self._raw)

    def add_comment(self, issue_key: str, body: str):
        self._raw.append(body)
        return {"id": len(self._raw)}

    def render_comment(self, document):
        return render_linkback(document, source=self.source)


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
        execution_id="exec-2",
        pr_url="https://ghes/acme/svc/pull/7",
        pr_title="Draft PR #7",
        repo="acme/svc",
        branch="agent/AGENTDEV-1/exec-2",
    )

    assert first.action == APPLIED
    assert second.action == SKIPPED
    assert len(adapter.comments) == 1
    body = adapter.comments[0]
    assert "[Draft PR #7|https://ghes/acme/svc/pull/7]" in body
    assert "rebar-exec" not in body


def test_cloud_linkback_renders_adf_json_without_marker():
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
    assert "rebar-exec" not in body
    assert "https://ghes/acme/svc/pull/9" in body


def test_cloud_linkback_dedupes_when_pr_url_present_in_stored_body():
    adapter = _FakeAdapter(CLOUD)
    adapter.comments.append("prior comment https://ghes/acme/svc/pull/9")

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


def test_linkback_does_not_dedupe_on_pr_number_prefix_collision():
    adapter = _FakeAdapter(DATACENTER)
    post_pr_linkback(
        adapter,
        "AGENTDEV-3",
        execution_id="exec-a",
        pr_url="https://ghes/acme/svc/pull/70",
        pr_title="Draft PR #70",
        repo="acme/svc",
        branch="agent/AGENTDEV-3/exec-a",
    )

    result = post_pr_linkback(
        adapter,
        "AGENTDEV-3",
        execution_id="exec-b",
        pr_url="https://ghes/acme/svc/pull/7",
        pr_title="Draft PR #7",
        repo="acme/svc",
        branch="agent/AGENTDEV-3/exec-b",
    )

    assert result.action == APPLIED
    assert len(adapter.comments) == 2


def test_cloud_linkback_dedupes_against_normalized_read_back():
    adapter = _CloudNormalizingAdapter()

    first = post_pr_linkback(
        adapter,
        "AGENTDEV-4",
        execution_id="exec-1",
        pr_url="https://ghes/acme/svc/pull/4",
        pr_title="[AGENTDEV-4] Add pagination",
        repo="acme/svc",
        branch="agent/AGENTDEV-4",
    )
    second = post_pr_linkback(
        adapter,
        "AGENTDEV-4",
        execution_id="exec-2",
        pr_url="https://ghes/acme/svc/pull/4",
        pr_title="ignored",
        repo="acme/svc",
        branch="agent/AGENTDEV-4",
    )

    assert first.action == APPLIED
    assert second.action == SKIPPED
    assert len(adapter._raw) == 1
