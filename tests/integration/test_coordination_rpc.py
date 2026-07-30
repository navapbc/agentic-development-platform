"""Coordination primitives served over HTTP and round-tripped by the client."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import asdict
from http.server import ThreadingHTTPServer

import pytest
from services.coordination import (
    ADMITTED,
    DISCARDED,
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
    SequencedAction,
    SideEffectRef,
    WorkUnit,
)
from services.coordination.service import build_handler
from services.integration.jira import IdempotentJiraWriter
from services.orchestration.coordination_client import CoordinationClient

TASK_ID = "AGENTDEV-1"
EXECUTION_ID = "exec-1"


@pytest.fixture
def coordination_url() -> Iterator[str]:
    handler = build_handler(
        InMemorySideEffectLedger(),
        InMemorySequenceGate(),
        InMemoryWorkClaimStore(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeWriteClient:
    def __init__(self) -> None:
        self.comments: list[str] = []

    def get_status(self, issue_key: str) -> str:
        return "To Do"

    def transition(self, issue_key: str, target_status: str) -> None:
        return None

    def list_comment_bodies(self, issue_key: str):
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str) -> None:
        self.comments.append(body)


async def test_idempotency_reserve_record_and_network_replay_is_a_noop(coordination_url: str):
    client = CoordinationClient(coordination_url)
    ref = SideEffectRef(TASK_ID, EXECUTION_ID, "jira_comment", TASK_ID)

    reserved = await client.reserve_side_effect(ref)
    assert reserved.replayed is False
    assert reserved.result is None

    recorded = await client.record_side_effect(ref, {"comment_id": "c1"})
    assert recorded.replayed is False
    assert recorded.result == {"comment_id": "c1"}

    replayed = await client.reserve_side_effect(ref)
    assert replayed.replayed is True
    assert replayed.result == {"comment_id": "c1"}
    assert replayed.key == reserved.key


async def test_run_once_over_the_network_runs_the_effect_exactly_once(coordination_url: str):
    client = CoordinationClient(coordination_url)
    ref = SideEffectRef(TASK_ID, EXECUTION_ID, "jira_comment", TASK_ID)
    writer = IdempotentJiraWriter(_FakeWriteClient())
    calls = {"n": 0}

    async def _effect():
        calls["n"] += 1
        return asdict(
            writer.post_comment(TASK_ID, "PR ready", execution_id=EXECUTION_ID, kind="pr_link")
        )

    first = await client.run_once(ref, _effect)
    replayed = await client.run_once(ref, _effect)

    assert first.replayed is False
    assert replayed.replayed is True
    assert replayed.result == first.result
    assert calls["n"] == 1


async def test_sequence_gate_admits_then_discards_stale_over_the_network(coordination_url: str):
    client = CoordinationClient(coordination_url)

    first = await client.admit_sequence(SequencedAction(TASK_ID, EXECUTION_ID, 1))
    second = await client.admit_sequence(SequencedAction(TASK_ID, EXECUTION_ID, 2))
    stale = await client.admit_sequence(SequencedAction(TASK_ID, EXECUTION_ID, 1))

    assert first.outcome == ADMITTED
    assert second.outcome == ADMITTED
    assert stale.outcome == DISCARDED


async def test_claim_is_won_once_and_next_batch_defers_file_collisions(coordination_url: str):
    client = CoordinationClient(coordination_url)
    unit_a = WorkUnit(TASK_ID, "agent-a", EXECUTION_ID, "agent-a", ("src/client.py",))
    unit_b = WorkUnit(TASK_ID, "agent-b", EXECUTION_ID, "agent-b", ("src/client.py",))

    won = await client.try_claim(unit_a)
    again = await client.try_claim(unit_a)
    assert won.won is True
    assert again.won is False
    assert again.claim_id == won.claim_id

    schedule = await client.next_batch([unit_a, unit_b])
    assert {u.work_unit for u in schedule.admitted} == {"agent-a"}
    assert {u.work_unit for u in schedule.deferred} == {"agent-b"}
