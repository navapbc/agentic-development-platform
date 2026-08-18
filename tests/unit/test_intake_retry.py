from __future__ import annotations

import pytest
from services.orchestration.intake_retry import IntakeRetryResult, retry_intake


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
    async def _check(workflow_id: str) -> bool:
        return workflow_id in running

    return _check


@pytest.mark.asyncio
async def test_retry_clears_dedup_and_marks_stale_execution_failed():
    store = _FakeStore(workflow_ids=["wf-1"], dedup=1)

    result = await retry_intake("ADSER-20", store=store, workflow_active=_active())

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

    await retry_intake("ADSER-99", store=store, workflow_active=_active())

    assert store.gate_cleared is False


@pytest.mark.asyncio
async def test_retry_refuses_when_a_workflow_is_still_running():
    store = _FakeStore(workflow_ids=["wf-live"], dedup=1)

    result = await retry_intake("ADSER-21", store=store, workflow_active=_active("wf-live"))

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

    result = await retry_intake("ADSER-99", store=store, workflow_active=_active())

    assert not result.retriggered
    assert result.released == 0
    assert store.marked is False
    assert "nothing to retrigger" in result.message


@pytest.mark.asyncio
async def test_retry_passes_repo_scope_to_the_store():
    store = _FakeStore(workflow_ids=[], dedup=1)

    await retry_intake(
        "ADSER-22", store=store, workflow_active=_active(), repo="agentic-demo-service"
    )

    assert store.cleared_repo == "agentic-demo-service"
