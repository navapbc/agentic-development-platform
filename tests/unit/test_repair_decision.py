from __future__ import annotations

import pytest
from services.orchestration.repair_decision import RepairDecisionResult, resume_repair


class _Store:
    def __init__(self, parked: list[str]) -> None:
        self._parked = parked
        self.asked: list[str] = []

    async def parked_workflow_ids(self, issue_key: str) -> list[str]:
        self.asked.append(issue_key)
        return self._parked


class _Signaller:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool, str]] = []

    async def signal_repair_decision(self, workflow_id: str, *, retry: bool, actor: str) -> None:
        self.sent.append((workflow_id, retry, actor))


@pytest.mark.asyncio
async def test_retry_decision_signals_the_parked_workflow():
    store = _Store(["draft-pr-ADSER-24-intake-39d2c862"])
    signaller = _Signaller()

    result = await resume_repair(
        "ADSER-24", retry=True, actor="operator", store=store, signaller=signaller
    )

    assert isinstance(result, RepairDecisionResult)
    assert result.resumed
    assert result.retry is True
    assert signaller.sent == [("draft-pr-ADSER-24-intake-39d2c862", True, "operator")]
    assert "sent a retry decision" in result.message


@pytest.mark.asyncio
async def test_abandon_decision_signals_the_parked_workflow():
    store = _Store(["draft-pr-ADSER-24-intake-39d2c862"])
    signaller = _Signaller()

    result = await resume_repair(
        "ADSER-24", retry=False, actor="release-manager", store=store, signaller=signaller
    )

    assert result.retry is False
    assert signaller.sent == [("draft-pr-ADSER-24-intake-39d2c862", False, "release-manager")]
    assert "sent a abandon decision" in result.message


@pytest.mark.asyncio
async def test_no_parked_workflow_is_a_noop():
    store = _Store([])
    signaller = _Signaller()

    result = await resume_repair(
        "ADSER-99", retry=True, actor="operator", store=store, signaller=signaller
    )

    assert not result.resumed
    assert signaller.sent == []
    assert "nothing to resume" in result.message


@pytest.mark.asyncio
async def test_every_parked_workflow_for_the_issue_receives_the_decision():
    store = _Store(["wf-a", "wf-b"])
    signaller = _Signaller()

    result = await resume_repair(
        "ADSER-24", retry=True, actor="operator", store=store, signaller=signaller
    )

    assert result.signalled == ("wf-a", "wf-b")
    assert [wid for wid, _, _ in signaller.sent] == ["wf-a", "wf-b"]
