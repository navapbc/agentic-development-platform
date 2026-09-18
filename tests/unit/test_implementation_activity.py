"""Tests for the implementation activity change-set hashing."""

from __future__ import annotations

from pathlib import Path

from openhands.runtime.conversation import COMPLETED, FINISHED, ImplementationOutcome
from services.orchestration.materialization import MaterializedWorkspace, StagedChange
from services.orchestration.temporal_activities import implementation as impl
from services.orchestration.temporal_activities.implementation import (
    ImplementationRequest,
    run_implementation_stage,
)

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
    monkeypatch.setattr(impl, "_build_runtime", lambda: runtime)

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
    monkeypatch.setattr(impl, "_build_runtime", lambda: runtime)

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
