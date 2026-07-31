"""Idempotent per-instance Jira linkback comment for an opened draft pull request."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from services.integration.jira.render import Block, heading, link, paragraph, render, text
from services.integration.jira.writes import APPLIED, SKIPPED, WriteResult, comment_marker

LINKBACK_KIND = "pr_link"


class LinkbackClient(Protocol):
    source: str

    def list_comment_bodies(self, issue_key: str) -> Sequence[str]: ...

    def add_comment(self, issue_key: str, body: str) -> Any: ...


def build_linkback_document(
    *, pr_url: str, pr_title: str, repo: str, branch: str, marker: str
) -> list[Block]:
    return [
        heading("Draft pull request opened"),
        paragraph(link(pr_title, pr_url), text(f" on {repo}@{branch}")),
        paragraph(text(marker)),
    ]


def render_linkback(document: list[Block], *, source: str) -> str:
    rendered = render(document, source)
    if isinstance(rendered, str):
        return rendered
    return json.dumps(rendered, separators=(",", ":"), sort_keys=True)


def post_pr_linkback(
    client: LinkbackClient,
    issue_key: str,
    *,
    execution_id: str,
    pr_url: str,
    pr_title: str,
    repo: str,
    branch: str,
    kind: str = LINKBACK_KIND,
) -> WriteResult:
    marker = comment_marker(execution_id, kind)
    if any(marker in existing for existing in client.list_comment_bodies(issue_key)):
        return WriteResult(SKIPPED, f"linkback {kind!r} already present")
    document = build_linkback_document(
        pr_url=pr_url, pr_title=pr_title, repo=repo, branch=branch, marker=marker
    )
    client.add_comment(issue_key, render_linkback(document, source=client.source))
    return WriteResult(APPLIED, f"linkback {kind!r} posted")
