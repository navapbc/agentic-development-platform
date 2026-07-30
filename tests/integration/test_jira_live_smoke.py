"""Optional live Jira smokes, skipped unless explicitly enabled.

Cloud: set RUN_LIVE_JIRA_CLOUD=1 with JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, and a vendored acli;
the Cloud smokes authenticate acli non-interactively when it is not already logged in.
Data Center: set RUN_LIVE_JIRA_DC=1 with JIRA_PAT. The read smokes are non-mutating.
RUN_LIVE_JIRA_CLOUD_WRITE=1 additionally runs a mutating Cloud smoke that creates an issue; it
runs before the read smoke, which reads that issue when no JIRA_SMOKE_ISSUE_KEY is set.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from scripts.config import Settings
from services.integration.jira import build_jira_adapter, ensure_acli_authenticated
from services.integration.jira.normalize import normalize_issue

_SMOKE_ISSUE_KEY = os.environ.get("JIRA_SMOKE_ISSUE_KEY", "")
_created_key = ""
_created_description = ""

requires_live_cloud = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_JIRA_CLOUD") != "1",
    reason="set RUN_LIVE_JIRA_CLOUD=1 with Cloud credentials and acli to run the Cloud smoke",
)
requires_live_cloud_write = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_JIRA_CLOUD_WRITE") != "1",
    reason="set RUN_LIVE_JIRA_CLOUD_WRITE=1 to run the mutating Cloud create smoke",
)
requires_live_datacenter = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_JIRA_DC") != "1",
    reason="set RUN_LIVE_JIRA_DC=1 with a Data Center PAT to run the connection smoke",
)


@requires_live_cloud_write
def test_cloud_creates_and_reads_issue():
    global _created_key, _created_description
    settings = Settings(jira_auth_mode="cloud")
    ensure_acli_authenticated(settings)
    adapter = build_jira_adapter(settings)
    marker = uuid4()
    summary = f"agentic-dev-platform cloud smoke {marker}"
    description = "\n".join(
        [
            f"# agentic-dev-platform cloud smoke {marker}",
            "",
            "Plain, **bold**, *italic*, and `inline code` on one line.",
            "",
            "## Bullet list",
            "",
            "- first item",
            "- second item",
            "- third item",
            "",
            "## Ordered list",
            "",
            "1. step one",
            "2. step two",
            "3. step three",
            "",
            "## Code block",
            "",
            "```",
            "def smoke():",
            "    return True",
            "```",
            "",
            "## Blockquote",
            "",
            "> quoted line one",
            "> quoted line two",
            "",
            "---",
            "",
            "Trailing paragraph after the rule.",
        ]
    )
    created = adapter.create_issue(
        {"ticket_type": "Task", "title": summary, "description": description}
    )
    task = normalize_issue(created)
    assert task.task_id
    assert task.summary == summary
    assert task.description == description
    _created_key = task.task_id
    _created_description = description


@requires_live_cloud
def test_cloud_issue_reads_and_normalizes():
    issue_key = _created_key or _SMOKE_ISSUE_KEY
    if not issue_key:
        pytest.skip("set JIRA_SMOKE_ISSUE_KEY or enable RUN_LIVE_JIRA_CLOUD_WRITE")
    settings = Settings(jira_auth_mode="cloud")
    ensure_acli_authenticated(settings)
    adapter = build_jira_adapter(settings)
    raw = adapter.get_issue(issue_key)
    task = normalize_issue(raw)
    assert task.task_id == issue_key
    assert task.summary
    if _created_description:
        assert task.description == _created_description


@requires_live_datacenter
def test_datacenter_connection_reads_and_normalizes():
    adapter = build_jira_adapter(Settings(jira_auth_mode="datacenter"))
    raw = adapter.get_issue(_SMOKE_ISSUE_KEY)
    task = normalize_issue(raw)
    assert task.task_id == _SMOKE_ISSUE_KEY
