from __future__ import annotations

import pytest
from services.orchestration.gate_decision import GateDecisionResult, resume_gate


class _Store:
    def __init__(self, parked: list[str]) -> None:
        self._parked = parked
        self.asked: list[str] = []

    async def parked_workflow_ids(self, issue_key: str) -> list[str]:
        self.asked.append(issue_key)
        return self._parked


class _Signaller:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool, str]] = []

    async def signal_gate_decision(
        self, workflow_id: str, *, gate: str, approved: bool, actor: str
    ) -> None:
        self.sent.append((workflow_id, gate, approved, actor))


@pytest.mark.asyncio
async def test_approve_decision_signals_the_parked_workflow():
    store = _Store(["draft-pr-ADSER-24-intake-39d2c862"])
    signaller = _Signaller()

    result = await resume_gate(
        "ADSER-24",
        gate="plan",
        approved=True,
        actor="tech-lead",
        store=store,
        signaller=signaller,
    )

    assert isinstance(result, GateDecisionResult)
    assert result.resumed
    assert result.approved is True
    assert signaller.sent == [("draft-pr-ADSER-24-intake-39d2c862", "plan", True, "tech-lead")]
    assert "sent a approve decision for gate plan" in result.message


@pytest.mark.asyncio
async def test_reject_decision_signals_the_parked_workflow():
    store = _Store(["draft-pr-ADSER-24-intake-39d2c862"])
    signaller = _Signaller()

    result = await resume_gate(
        "ADSER-24",
        gate="security-review",
        approved=False,
        actor="security-lead",
        store=store,
        signaller=signaller,
    )

    assert result.approved is False
    assert signaller.sent == [
        ("draft-pr-ADSER-24-intake-39d2c862", "security-review", False, "security-lead")
    ]
    assert "sent a reject decision for gate security-review" in result.message


@pytest.mark.asyncio
async def test_no_parked_workflow_is_a_noop():
    store = _Store([])
    signaller = _Signaller()

    result = await resume_gate(
        "ADSER-99",
        gate="plan",
        approved=True,
        actor="operator",
        store=store,
        signaller=signaller,
    )

    assert not result.resumed
    assert signaller.sent == []
    assert "nothing to resume" in result.message


@pytest.mark.asyncio
async def test_every_parked_workflow_for_the_issue_receives_the_decision():
    store = _Store(["wf-a", "wf-b"])
    signaller = _Signaller()

    result = await resume_gate(
        "ADSER-24",
        gate="plan",
        approved=True,
        actor="operator",
        store=store,
        signaller=signaller,
    )

    assert result.signalled == ("wf-a", "wf-b")
    assert [wid for wid, *_ in signaller.sent] == ["wf-a", "wf-b"]
