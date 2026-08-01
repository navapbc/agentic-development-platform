"""Role selection and worker construction for the Temporal worker entry point."""

from __future__ import annotations

import pytest
from services.orchestration.worker import (
    WORKER_ROLE_ACTIVITY,
    WORKER_ROLE_ALL,
    WORKER_ROLE_WORKFLOW,
    _resolve_role,
    build_worker,
)
from temporalio.testing import WorkflowEnvironment


def test_resolve_role_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_ROLE", raising=False)
    assert _resolve_role() == WORKER_ROLE_ALL


def test_resolve_role_normalizes_case_and_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ROLE", "  Workflow  ")
    assert _resolve_role() == WORKER_ROLE_WORKFLOW


def test_resolve_role_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ROLE", "bogus")
    with pytest.raises(SystemExit):
        _resolve_role()


async def test_build_worker_workflow_role_registers_only_workflows() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(env.client, "q-workflow", WORKER_ROLE_WORKFLOW)
        config = worker.config()
        assert config["workflows"]
        assert not config["activities"]


async def test_build_worker_activity_role_registers_only_activities() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(env.client, "q-activity", WORKER_ROLE_ACTIVITY)
        config = worker.config()
        assert config["activities"]
        assert not config["workflows"]


async def test_build_worker_all_role_registers_both() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(env.client, "q-all", WORKER_ROLE_ALL)
        config = worker.config()
        assert config["workflows"]
        assert config["activities"]
