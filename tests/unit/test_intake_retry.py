from __future__ import annotations

from types import SimpleNamespace

import pytest
from services.orchestration.intake_retry import (
    IntakeRetryResult,
    WorkflowLiveness,
    retry_intake,
    temporal_workflow_liveness,
)


class _FakeStore:
    def __init__(self, *, workflow_ids: list[str], dedup: int) -> None:
        self._workflow_ids = workflow_ids
        self._dedup = dedup
        self.cleared_repo: str | None = "unset"
        self.marked = False
        self.gate_cleared = False

    async def workflow_ids(self, issue_key: str) -> list[str]:
        return self._workflow_ids

    async def clear_intake_dedup(self, issue_key: str, repo: str | None) -> int:
        self.cleared_repo = repo
        return self._dedup

    async def mark_non_terminal_failed(self, issue_key: str) -> int:
        self.marked = True
        return 1

    async def clear_sequence_gate(self, issue_key: str) -> int:
        self.gate_cleared = True
        return 1


def _active(*running: str):
    async def _check(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.RUNNING if workflow_id in running else WorkflowLiveness.ENDED

    return _check


def _undetermined():
    async def _check(workflow_id: str) -> WorkflowLiveness:
        return WorkflowLiveness.UNKNOWN

    return _check


@pytest.mark.asyncio
async def test_retry_clears_dedup_and_marks_stale_execution_failed():
    store = _FakeStore(workflow_ids=["wf-1"], dedup=1)

    result = await retry_intake("ADSER-20", store=store, liveness=_active())

    assert isinstance(result, IntakeRetryResult)
    assert result.retriggered
    assert result.released == 1
    assert result.superseded == 1
    assert store.marked is True
    assert store.gate_cleared is True
    assert "cleared for retrigger" in result.message


@pytest.mark.asyncio
async def test_retry_does_not_reset_the_sequence_gate_when_nothing_is_released():
    store = _FakeStore(workflow_ids=[], dedup=0)

    await retry_intake("ADSER-99", store=store, liveness=_active())

    assert store.gate_cleared is False


@pytest.mark.asyncio
async def test_retry_refuses_when_a_workflow_is_still_running():
    store = _FakeStore(workflow_ids=["wf-live"], dedup=1)

    result = await retry_intake("ADSER-21", store=store, liveness=_active("wf-live"))

    assert not result.retriggered
    assert result.blocked_by == ("wf-live",)
    assert result.released == 0
    assert store.cleared_repo == "unset"
    assert store.marked is False
    assert store.gate_cleared is False
    assert "still has a running workflow" in result.message


@pytest.mark.asyncio
async def test_retry_with_no_intake_record_reports_nothing_to_do():
    store = _FakeStore(workflow_ids=[], dedup=0)

    result = await retry_intake("ADSER-99", store=store, liveness=_active())

    assert not result.retriggered
    assert result.released == 0
    assert store.marked is False
    assert "nothing to retrigger" in result.message


@pytest.mark.asyncio
async def test_retry_passes_repo_scope_to_the_store():
    store = _FakeStore(workflow_ids=[], dedup=1)

    await retry_intake(
        "ADSER-22", store=store, liveness=_active(), repo="agentic-demo-service"
    )

    assert store.cleared_repo == "agentic-demo-service"


@pytest.mark.asyncio
async def test_unreachable_temporal_refuses_without_claiming_a_workflow_runs():
    store = _FakeStore(workflow_ids=["wf-unreachable"], dedup=1)

    result = await retry_intake("ADSER-23", store=store, liveness=_undetermined())

    assert not result.retriggered
    assert result.blocked_by == ()
    assert result.undetermined == ("wf-unreachable",)
    assert result.released == 0
    assert store.marked is False
    assert store.gate_cleared is False
    assert "still has a running workflow" not in result.message
    assert "could not determine" in result.message


@pytest.mark.asyncio
async def test_a_confirmed_running_workflow_outranks_an_undetermined_one():
    store = _FakeStore(workflow_ids=["wf-live", "wf-unreachable"], dedup=1)

    async def _mixed(workflow_id: str) -> WorkflowLiveness:
        return (
            WorkflowLiveness.RUNNING
            if workflow_id == "wf-live"
            else WorkflowLiveness.UNKNOWN
        )

    result = await retry_intake("ADSER-24", store=store, liveness=_mixed)

    assert result.blocked_by == ("wf-live",)
    assert result.undetermined == ("wf-unreachable",)
    assert "still has a running workflow" in result.message


@pytest.mark.asyncio
async def test_temporal_probe_reports_unknown_when_the_connection_fails(monkeypatch):
    from temporalio.client import Client

    async def _boom(*args, **kwargs):
        raise OSError("connect call failed")

    monkeypatch.setattr(Client, "connect", _boom)
    settings = SimpleNamespace(temporal_host="temporal:7233", temporal_namespace="default")

    probe = temporal_workflow_liveness(settings)

    assert await probe("wf-x") is WorkflowLiveness.UNKNOWN
