"""Optional live Jira smokes, skipped unless explicitly enabled.

Configure the Cloud and Data Center sites in config/<env>/jira.yaml and export the tokens the
config references, for example JIRA_API_TOKEN and JIRA_PAT, in .env.

Cloud smokes run when RUN_LIVE_JIRA_CLOUD=1 and a vendored acli is present. They authenticate
acli non-interactively when it is not already logged in. Data Center smokes run when
RUN_LIVE_JIRA_DC=1. The read smokes are non-mutating.

RUN_LIVE_JIRA_CLOUD_WRITE=1 additionally runs a mutating Cloud smoke that creates an issue. It
runs before the read smoke, which reads that issue when no JIRA_SMOKE_ISSUE_KEY is set. The read
retries briefly for a freshly created key to tolerate Jira Cloud JQL index lag.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from scripts.config import Settings
from services.integration.jira import build_jira_adapter, ensure_acli_authenticated
from services.integration.jira.adapter import project_of
from services.integration.jira.credentials import build_jira_credential_resolver
from services.integration.jira.normalize import normalize_issue

_SMOKE_ISSUE_KEY = os.environ.get("JIRA_SMOKE_ISSUE_KEY", "")
_created_key = ""
_created_description = ""


def _smoke_project(settings: Settings) -> str:
    projects = sorted(settings.allowed_projects())
    return projects[0] if projects else ""


def _live_adapter(settings: Settings, project: str):
    return build_jira_adapter(settings, project=project)


def _acli_login(settings: Settings, project: str) -> None:
    cred = build_jira_credential_resolver(settings).resolve(project)
    if cred is not None:
        ensure_acli_authenticated(cred, acli_path=settings.jira_acli_path)


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


def _read_created_issue(adapter, issue_key, *, attempts=6, delay=2.0):
    last_exc: RuntimeError | None = None
    for attempt in range(attempts):
        try:
            return adapter.get_issue(issue_key)
        except RuntimeError as exc:
            # Jira Cloud JQL index lags after create; a fresh key reads as not-found briefly.
            if "not found" not in str(exc).lower():
                raise
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


@requires_live_cloud_write
def test_cloud_creates_and_reads_issue():
    global _created_key, _created_description
    settings = Settings()
    project = _smoke_project(settings)
    _acli_login(settings, project)
    adapter = _live_adapter(settings, project)
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
    settings = Settings()
    _acli_login(settings, project_of(issue_key))
    adapter = _live_adapter(settings, project_of(issue_key))
    raw = _read_created_issue(adapter, issue_key) if _created_key else adapter.get_issue(issue_key)
    task = normalize_issue(raw)
    assert task.task_id == issue_key
    assert task.summary
    if _created_description:
        assert task.description == _created_description


@requires_live_datacenter
def test_datacenter_connection_reads_and_normalizes():
    adapter = _live_adapter(Settings(), project_of(_SMOKE_ISSUE_KEY))
    raw = adapter.get_issue(_SMOKE_ISSUE_KEY)
    task = normalize_issue(raw)
    assert task.task_id == _SMOKE_ISSUE_KEY
