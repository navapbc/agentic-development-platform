from __future__ import annotations

from subprocess import CalledProcessError
from typing import Any

import pytest
from services.orchestration import intake_runtime
from services.orchestration.intake_base import UnresolvableBaseBranch
from services.orchestration.intake_reconciler import IntakeStart
from services.orchestration.intake_runtime import TemporalStarter
from services.orchestration.usage_rollup import BudgetCaps
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
    assert arg.review_task_queue == "agent-platform-review"


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


def _intake_start(**overrides: Any) -> IntakeStart:
    fields: dict[str, Any] = {
        "task_id": "ADEV-1",
        "execution_id": "exec-1",
        "repo": "agentic-demo-service",
        "issue_key": "ADEV-1",
        "summary": "Add a widget",
        "jira_project": "ADEV",
        "stored_issue": {},
    }
    fields.update(overrides)
    return IntakeStart(**fields)


@pytest.mark.asyncio
async def test_start_uses_the_declared_base_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []

    def _remote_head(remote: str, branch: str) -> str:
        looked_up.append(branch)
        return "feature-sha" if branch == "feature-x" else "main-sha"

    monkeypatch.setattr(intake_runtime, "remote_head", _remote_head)
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    await starter.start(_intake_start(base="feature-x"))

    (_, arg, _) = client.started[0]
    assert arg.base == "feature-x"
    assert looked_up == ["feature-x"]


@pytest.mark.asyncio
async def test_declared_base_sha_is_resolved_from_that_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intake_runtime,
        "remote_head",
        lambda remote, branch: "feature-sha" if branch == "feature-x" else "main-sha",
    )
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    await starter.start(_intake_start(base="feature-x"))

    (_, arg, _) = client.started[0]
    assert arg.base_sha == "feature-sha"


@pytest.mark.asyncio
async def test_start_falls_back_to_the_default_base_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "main-sha")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    settings = hermetic_settings()
    starter = TemporalStarter(client, settings, _Gh())

    await starter.start(_intake_start())

    (_, arg, _) = client.started[0]
    assert arg.base == settings.github_default_base_branch
    assert arg.base_sha == "main-sha"


@pytest.mark.asyncio
async def test_unresolvable_declared_base_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    with pytest.raises(UnresolvableBaseBranch) as caught:
        await starter.start(_intake_start(base="gone"))

    assert caught.value.branch == "gone"
    assert "gone" in str(caught.value)
    assert client.started == []


@pytest.mark.asyncio
async def test_remote_lookup_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(remote: str, branch: str) -> str:
        raise CalledProcessError(128, ["git", "ls-remote"])

    monkeypatch.setattr(intake_runtime, "remote_head", _boom)
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    starter = TemporalStarter(_Client(), hermetic_settings(), _Gh())

    with pytest.raises(CalledProcessError):
        await starter.start(_intake_start(base="feature-x"))


@pytest.mark.asyncio
async def test_resolve_base_returns_the_remote_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: f"sha-{branch}")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    starter = TemporalStarter(_Client(), hermetic_settings(), _Gh())

    assert await starter.resolve_base(repo="agentic-demo-service", branch="feature-x") == (
        "sha-feature-x"
    )


@pytest.mark.asyncio
async def test_resolve_base_returns_empty_for_a_missing_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    starter = TemporalStarter(_Client(), hermetic_settings(), _Gh())

    assert await starter.resolve_base(repo="agentic-demo-service", branch="gone") == ""


@pytest.mark.asyncio
async def test_start_does_not_raise_when_the_default_base_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    await starter.start(_intake_start())

    (_, arg, _) = client.started[0]
    assert arg.base_sha == ""


@pytest.mark.asyncio
async def test_start_derives_the_commit_message_from_the_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "main-sha")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    await starter.start(
        _intake_start(summary="Add a widget", description="The widget is needed for onboarding.")
    )

    (_, arg, _) = client.started[0]
    assert arg.commit_message == "ADEV-1: Add a widget\n\nThe widget is needed for onboarding."


@pytest.mark.asyncio
async def test_start_prefers_a_declared_tracker_key_in_the_commit_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "main-sha")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    await starter.start(
        _intake_start(
            summary="Add a widget",
            description="The widget is needed for onboarding.",
            stored_issue={"labels": ["adp", "jira-pfmlpb-33739"]},
        )
    )

    (_, arg, _) = client.started[0]
    assert arg.commit_message.startswith("PFMLPB-33739: Add a widget")
    assert arg.issue_key == "ADEV-1"


@pytest.mark.asyncio
async def test_start_carries_the_declared_file_impact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "abc123")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

    await starter.start(
        IntakeStart(
            task_id="ADEV-1",
            execution_id="exec-1",
            repo="agentic-demo-service",
            issue_key="ADEV-1",
            summary="Add a widget",
            jira_project="ADEV",
            stored_issue={},
            file_impact=("portal/src/widget.tsx",),
        )
    )

    (_, arg, _) = client.started[0]
    assert arg.file_impact == ["portal/src/widget.tsx"]


@pytest.mark.asyncio
async def test_start_leaves_file_impact_empty_when_undeclared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "abc123")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
    starter = TemporalStarter(client, hermetic_settings(), _Gh())

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
    assert arg.file_impact == []


async def _started_input(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Any:
    monkeypatch.setattr(intake_runtime, "remote_head", lambda remote, branch: "abc123")
    monkeypatch.setattr(
        "services.integration.github.factory.build_git_host", lambda settings: _Host()
    )
    client = _Client()
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
    return arg


@pytest.mark.asyncio
async def test_intake_carries_the_configured_llm_call_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arg = await _started_input(monkeypatch, hermetic_settings(implementation_max_llm_calls=640))

    assert arg.max_llm_calls == 640


@pytest.mark.asyncio
async def test_intake_carries_the_configured_token_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arg = await _started_input(monkeypatch, hermetic_settings(implementation_max_tokens=7_500_000))

    assert arg.max_tokens == 7_500_000


@pytest.mark.asyncio
async def test_intake_carries_the_configured_cost_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arg = await _started_input(monkeypatch, hermetic_settings(implementation_max_cost_usd=42.5))

    assert arg.max_cost_usd == 42.5


@pytest.mark.asyncio
async def test_intake_falls_back_to_the_default_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = BudgetCaps()

    arg = await _started_input(monkeypatch, hermetic_settings())

    assert (arg.max_llm_calls, arg.max_tokens, arg.max_cost_usd) == (
        caps.max_llm_calls,
        caps.max_tokens,
        caps.max_cost_usd,
    )
