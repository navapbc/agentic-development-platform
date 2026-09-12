from __future__ import annotations

import pytest
from adapters.interfaces.git_host import PublishResult
from scripts.operator_done import (
    AllowlistAuthorizer,
    run_operator_done,
)


class _Host:
    def __init__(self, publish: PublishResult | None) -> None:
        self._publish = publish

    async def find_published_branch(self, repo: str, head: str) -> PublishResult | None:
        return self._publish


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, event: dict) -> None:
        self.records.append(event)

    def events(self) -> list[str]:
        return [r["event"] for r in self.records]


class _Ledger:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str) -> None:
        self._seen.add(key)


def _publish() -> PublishResult:
    return PublishResult(
        repo="demo",
        branch="agent/T-1",
        completion_signal_id="7",
        summary_path="/x/SUMMARY.md",
        patch_path="/x/change.patch",
    )


async def _run(*, actor, host, ledger, audit, expected="", merged=True, signals):
    async def signaler(workflow_id: str, m: bool) -> None:
        signals.append((workflow_id, m))

    return await run_operator_done(
        workflow_id="wf-1",
        repo="demo",
        branch="agent/T-1",
        actor=actor,
        host=host,
        authorizer=AllowlistAuthorizer(frozenset({"alice"})),
        audit=audit,
        signaler=signaler,
        ledger=ledger,
        merged=merged,
        expected_completion_signal_id=expected,
    )


@pytest.mark.asyncio
async def test_unauthorized_operator_is_rejected_and_audited() -> None:
    audit = _Audit()
    signals: list = []
    result = await _run(
        actor="mallory", host=_Host(_publish()), ledger=_Ledger(), audit=audit, signals=signals
    )
    assert result.authorized is False
    assert result.signaled is False
    assert signals == []
    assert "operator_done_denied" in audit.events()


@pytest.mark.asyncio
async def test_missing_publish_does_not_complete() -> None:
    audit = _Audit()
    signals: list = []
    result = await _run(
        actor="alice", host=_Host(None), ledger=_Ledger(), audit=audit, signals=signals
    )
    assert result.verified is False
    assert result.reason == "no_publish"
    assert signals == []


@pytest.mark.asyncio
async def test_identifier_mismatch_does_not_complete() -> None:
    audit = _Audit()
    signals: list = []
    result = await _run(
        actor="alice",
        host=_Host(_publish()),
        ledger=_Ledger(),
        audit=audit,
        expected="999",
        signals=signals,
    )
    assert result.verified is False
    assert result.reason == "identifier_mismatch"
    assert signals == []


@pytest.mark.asyncio
async def test_authorized_verified_operator_signals_completion() -> None:
    audit = _Audit()
    signals: list = []
    result = await _run(
        actor="alice",
        host=_Host(_publish()),
        ledger=_Ledger(),
        audit=audit,
        expected="7",
        signals=signals,
    )
    assert result.signaled is True
    assert signals == [("wf-1", True)]
    assert "operator_done_signaled" in audit.events()


@pytest.mark.asyncio
async def test_repeat_is_idempotent() -> None:
    audit = _Audit()
    signals: list = []
    ledger = _Ledger()
    host = _Host(_publish())
    first = await _run(actor="alice", host=host, ledger=ledger, audit=audit, signals=signals)
    second = await _run(actor="alice", host=host, ledger=ledger, audit=audit, signals=signals)
    assert first.signaled is True
    assert second.signaled is False
    assert second.replayed is True
    assert signals == [("wf-1", True)]
