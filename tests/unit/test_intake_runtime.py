from __future__ import annotations

from typing import Any

import pytest
from services.orchestration import intake_runtime
from services.orchestration.intake_reconciler import IntakeStart
from services.orchestration.intake_runtime import TemporalStarter
from tests.support import hermetic_settings


class _Client:
    def __init__(self) -> None:
        self.started: list[tuple[Any, Any, dict[str, Any]]] = []

    async def start_workflow(self, run: Any, arg: Any, **kwargs: Any) -> None:
        self.started.append((run, arg, kwargs))


class _Gh:
    def git_remote_url(self, repo: str) -> str:
        return f"https://example.test/{repo}.git"


class _Host:
    def source_location(self, repo: str) -> str:
        return f"https://example.test/{repo}.git"


@pytest.mark.asyncio
async def test_start_sets_run_flags_and_no_ci_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "abc123")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    workflow_id = await starter.start(
        IntakeStart(
            task_id="ADEV-1",
            execution_id="exec-1",
            repo="agentic-demo-service",
            issue_key="ADEV-1",
            summary="Add a widget",
            jira_project="ADEV",
            stored_issue={},
        )
    )

    assert workflow_id == "draft-pr-ADEV-1-exec-1"
    (_, arg, _) = client.started[0]
    assert not hasattr(arg, "ci_job")
    assert arg.run_reviews is False
    assert arg.run_policy is False
    assert arg.run_approval is False
    assert arg.run_planning is True
    assert arg.await_pr_close is False


@pytest.mark.asyncio
async def test_start_awaits_pr_close_for_local_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "abc123")
    (tmp_path / "agentic-demo-service").mkdir()
    client = _Client()
    settings = hermetic_settings(
        source_control_backend="local",
        source_control_local_root=str(tmp_path),
        source_control_local_repos="agentic-demo-service",
    )
    starter = TemporalStarter(client, settings, _Gh())

    await starter.start(
        IntakeStart(
            task_id="ADEV-1",
            execution_id="exec-1",
            repo="agentic-demo-service",
            issue_key="ADEV-1",
            summary="Add a widget",
            jira_project="ADEV",
            stored_issue={},
        )
    )

    (_, arg, _) = client.started[0]
    assert arg.await_pr_close is True
