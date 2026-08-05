"""Tests for the implementation activity change-set hashing."""

from __future__ import annotations

from pathlib import Path

from openhands.runtime.conversation import COMPLETED, FINISHED, ImplementationOutcome
from services.orchestration.materialization import MaterializedWorkspace
from services.orchestration.temporal_activities import implementation as impl
from services.orchestration.temporal_activities.implementation import (
    ImplementationRequest,
    _hash_changes,
    run_implementation_stage,
)


def test_hash_changes_is_order_independent():
    a = _hash_changes({"b.py": "two", "a.py": "one"})
    b = _hash_changes({"a.py": "one", "b.py": "two"})
    assert a == b
    assert a.startswith("sha256:")


def test_hash_changes_is_content_sensitive():
    base = _hash_changes({"a.py": "one"})
    changed_content = _hash_changes({"a.py": "two"})
    changed_path = _hash_changes({"b.py": "one"})
    assert base != changed_content
    assert base != changed_path


def test_hash_changes_empty_is_stable():
    assert _hash_changes({}) == _hash_changes({})


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

    async def collect_changes(self, workspace):
        return {"feature.py": "print('x')"}


class _FakeRuntime:
    def __init__(self) -> None:
        self.received: dict[str, str] = {}

    async def run_implementation(
        self, *, execution_id, task, context="", workspace_sub_path="", task_id=""
    ):
        self.received = {
            "execution_id": execution_id,
            "workspace_sub_path": workspace_sub_path,
            "task_id": task_id,
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

    assert runtime.received["workspace_sub_path"] == "agentic-demo-service/AGENTDEV-1/exec-1"
    assert result.terminal_reason == COMPLETED
    assert result.tokens == 100
    assert result.llm_calls == 3
    assert result.changed_files == ["feature.py"]
