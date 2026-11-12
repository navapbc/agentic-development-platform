"""Tests for the implementation activity change-set hashing."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
from openhands.runtime.conversation import COMPLETED, FINISHED, ImplementationOutcome
from services.orchestration.gates import image_admission
from services.orchestration.materialization import MaterializedWorkspace, StagedChange
from services.orchestration.temporal_activities import implementation as impl
from services.orchestration.temporal_activities.implementation import (
    ImplementationRequest,
    run_implementation_stage,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment


async def _admit_any(image, settings, ops=None):
    return None


_STAGED_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class _FakeMaterializer:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root

    async def materialize(self, *, repo, task_id, execution_id, base, commit_sha):
        path = self.workspace_root / repo / task_id / execution_id
        return MaterializedWorkspace(
            repo=repo,
            task_id=task_id,
            execution_id=execution_id,
            branch=f"agent/{task_id}",
            base=base,
            commit_sha=commit_sha,
            path=str(path),
            branch_orphan_id="b",
            workspace_orphan_id="w",
        )

    async def stage(self, workspace):
        return StagedChange(tree=_STAGED_TREE, changed=True)

    async def changed_paths(self, workspace):
        return ["feature.py"]


class _FakeRuntime:
    image = "sandbox@sha256:" + "a" * 64

    def __init__(self) -> None:
        self.received: dict[str, str] = {}

    async def run_implementation(
        self, *, execution_id, task, context="", workspace_source="", task_id=""
    ):
        self.received = {
            "execution_id": execution_id,
            "workspace_source": workspace_source,
            "task_id": task_id,
            "context": context,
        }
        return ImplementationOutcome(
            status=FINISHED,
            terminal_reason=COMPLETED,
            tool_calls=2,
            elapsed_seconds=1.0,
            cost=0.5,
            tokens=100,
            llm_calls=3,
            summary="ok",
        )


async def test_stage_binds_the_materialized_sub_path_into_the_runtime(tmp_path, monkeypatch):
    materializer = _FakeMaterializer(tmp_path)
    runtime = _FakeRuntime()
    monkeypatch.setattr(impl, "_build_materializer", lambda: materializer)
    monkeypatch.setattr(impl, "_build_runtime", lambda repo="": runtime)
    monkeypatch.setattr(image_admission, "admit_sandbox_image", _admit_any)

    result = await run_implementation_stage(
        ImplementationRequest(
            task_id="AGENTDEV-1",
            execution_id="exec-1",
            repo="agentic-demo-service",
            base="main",
            commit="a" * 40,
            task="Implement",
        )
    )

    assert runtime.received["workspace_source"] == str(
        (tmp_path / "agentic-demo-service" / "AGENTDEV-1" / "exec-1").resolve()
    )
    assert result.terminal_reason == COMPLETED
    assert result.tokens == 100
    assert result.llm_calls == 3
    assert result.changed_files == ["feature.py"]
    assert result.diff_hash == _STAGED_TREE


async def test_stage_appends_the_plan_to_the_coder_context(tmp_path, monkeypatch):
    materializer = _FakeMaterializer(tmp_path)
    runtime = _FakeRuntime()
    monkeypatch.setattr(impl, "_build_materializer", lambda: materializer)
    monkeypatch.setattr(impl, "_build_runtime", lambda repo="": runtime)
    monkeypatch.setattr(image_admission, "admit_sandbox_image", _admit_any)

    await run_implementation_stage(
        ImplementationRequest(
            task_id="AGENTDEV-1",
            execution_id="exec-1",
            repo="agentic-demo-service",
            base="main",
            commit="a" * 40,
            task="Implement",
            context="briefing pack",
            plan="Edit feature.py",
        )
    )

    assert runtime.received["context"] == "briefing pack\n\nApproved change plan:\nEdit feature.py"


def _request() -> ImplementationRequest:
    return ImplementationRequest(
        task_id="AGENTDEV-1",
        execution_id="exec-1",
        repo="agentic-demo-service",
        base="main",
        commit="a" * 40,
        task="Implement",
    )


def _retry_environment(previous_attempt: timedelta) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        attempt=2,
        start_to_close_timeout=timedelta(hours=1),
        current_attempt_scheduled_time=env.info.scheduled_time + previous_attempt,
    )
    return env


class _CleaningMaterializer(_FakeMaterializer):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.discarded: list[str] = []

    async def discard_changes(self, workspace):
        self.discarded.append(workspace.path)


async def test_a_retry_after_a_lost_worker_runs_the_stage_on_a_clean_workspace(
    tmp_path, monkeypatch
):
    materializer = _CleaningMaterializer(tmp_path)
    monkeypatch.setattr(impl, "_build_materializer", lambda: materializer)
    monkeypatch.setattr(impl, "_build_runtime", lambda repo="": _FakeRuntime())
    monkeypatch.setattr(image_admission, "admit_sandbox_image", _admit_any)

    result = await _retry_environment(timedelta(minutes=12)).run(
        run_implementation_stage, _request()
    )

    assert result.terminal_reason == COMPLETED
    assert len(materializer.discarded) == 1


async def test_a_retry_after_the_stage_timed_out_refuses_to_run_again(tmp_path, monkeypatch):
    materializer = _CleaningMaterializer(tmp_path)
    monkeypatch.setattr(impl, "_build_materializer", lambda: materializer)

    with pytest.raises(ApplicationError) as refused:
        await _retry_environment(timedelta(hours=1, seconds=1)).run(
            run_implementation_stage, _request()
        )

    assert refused.value.non_retryable is True
    assert refused.value.type == impl.IMPLEMENTATION_TIMED_OUT_ERROR_TYPE
    assert materializer.discarded == []


async def test_a_stage_failure_is_not_retried(tmp_path, monkeypatch):
    class _FailingRuntime(_FakeRuntime):
        async def run_implementation(self, **kwargs):
            raise RuntimeError("sandbox pod entered terminal phase Failed")

    monkeypatch.setattr(impl, "_build_materializer", lambda: _FakeMaterializer(tmp_path))
    monkeypatch.setattr(impl, "_build_runtime", lambda repo="": _FailingRuntime())
    monkeypatch.setattr(image_admission, "admit_sandbox_image", _admit_any)

    with pytest.raises(ApplicationError) as failed:
        await ActivityEnvironment().run(run_implementation_stage, _request())

    assert failed.value.non_retryable is True
    assert failed.value.type == "RuntimeError"
    assert "terminal phase Failed" in failed.value.message
