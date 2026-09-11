"""Idempotent per-instance Jira linkback comment for an opened draft pull request."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from services.integration.jira.render import Block, heading, link, paragraph, render, text
from services.integration.jira.writes import APPLIED, SKIPPED, WriteResult

LINKBACK_KIND = "pr_link"


class LinkbackClient(Protocol):
    def list_comment_bodies(self, issue_key: str) -> Sequence[str]: ...

    def add_comment(self, issue_key: str, body: str) -> Any: ...

    def render_comment(self, document: list[Block]) -> str: ...


def build_linkback_document(*, pr_url: str, pr_title: str, repo: str, branch: str) -> list[Block]:
    return [
        heading("Draft pull request opened"),
        paragraph(link(pr_title, pr_url), text(f" on {repo}@{branch} ({pr_url})")),
    ]


def render_linkback(document: list[Block], *, source: str) -> str:
    rendered = render(document, source)
    if isinstance(rendered, str):
        return rendered
    return json.dumps(rendered, separators=(",", ":"), sort_keys=True)


def _comment_mentions_url(body: str, url: str) -> bool:
    start = body.find(url)
    while start != -1:
        following = body[start + len(url) : start + len(url) + 1]
        if not following.isdigit():
            return True
        start = body.find(url, start + len(url))
    return False


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
    existing_bodies = client.list_comment_bodies(issue_key)
    if any(_comment_mentions_url(body, pr_url) for body in existing_bodies):
        return WriteResult(SKIPPED, f"linkback {kind!r} already present")
    document = build_linkback_document(pr_url=pr_url, pr_title=pr_title, repo=repo, branch=branch)
    client.add_comment(issue_key, client.render_comment(document))
    return WriteResult(APPLIED, f"linkback {kind!r} posted")
