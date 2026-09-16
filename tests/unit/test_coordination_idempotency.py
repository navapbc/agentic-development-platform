"""Idempotency-primitive tests: deterministic keys and replay-is-a-no-op semantics."""

from __future__ import annotations

from services.coordination.idempotency import (
    InMemorySideEffectLedger,
    SideEffectRef,
    side_effect_key,
)


def test_key_is_deterministic_and_field_sensitive():
    base = SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "In Progress")

    assert side_effect_key(base) == side_effect_key(
        SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "In Progress")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "Done")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-1", "e2", "jira_transition", "In Progress")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-1", "e1", "jira_comment", "In Progress")
    )
    assert side_effect_key(base) != side_effect_key(
        SideEffectRef("AGENTDEV-2", "e1", "jira_transition", "In Progress")
    )


async def test_replay_returns_recorded_result_without_rerunning_effect():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "jira_transition", "In Progress")
    calls: list[int] = []

    def effect():
        calls.append(1)
        return {"transitioned": True}

    first = await ledger.run_once(ref, effect)
    second = await ledger.run_once(ref, effect)

    assert first.replayed is False
    assert second.replayed is True
    assert len(calls) == 1
    assert second.result == {"transitioned": True}
    assert first.key == second.key


async def test_distinct_refs_each_execute():
    ledger = InMemorySideEffectLedger()
    calls: list[str] = []

    async def make(target: str):
        ref = SideEffectRef("AGENTDEV-1", "e1", "jira_comment", target)

        def effect():
            calls.append(target)
            return {"target": target}

        return await ledger.run_once(ref, effect)

    a = await make("pr_link")
    b = await make("summary")

    assert a.replayed is False
    assert b.replayed is False
    assert calls == ["pr_link", "summary"]


async def test_supports_async_effect_for_evidence_writes():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "evidence_write", "content-hash-1")
    writes: list[str] = []

    async def persist():
        writes.append("content-hash-1")
        return {"artifact_id": "a1"}

    first = await ledger.run_once(ref, persist)
    second = await ledger.run_once(ref, persist)

    assert first.result == {"artifact_id": "a1"}
    assert second.replayed is True
    assert writes == ["content-hash-1"]


async def test_reserving_a_still_pending_ref_again_reports_reentry():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "github.commit", "demo:agent/AGENTDEV-1")

    first = await ledger.reserve(ref)
    second = await ledger.reserve(ref)

    assert (first.replayed, first.reentrant) == (False, False)
    assert (second.replayed, second.reentrant) == (False, True)


async def test_reserving_after_completion_replays_rather_than_reentering():
    ledger = InMemorySideEffectLedger()
    ref = SideEffectRef("AGENTDEV-1", "e1", "github.commit", "demo:agent/AGENTDEV-1")

    await ledger.reserve(ref)
    await ledger.record(ref, {"sha": "abc"})
    outcome = await ledger.reserve(ref)

    assert outcome.replayed is True
    assert outcome.reentrant is False
    assert outcome.result == {"sha": "abc"}
