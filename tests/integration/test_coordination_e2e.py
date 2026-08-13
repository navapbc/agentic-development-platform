"""Exit proof: two agents run non-colliding units with the full coordination stack.

Hermetic. Composes Jira-canonical intake, the determinism manifest, atomic claims, the
file-impact scheduler, the sequence gate, idempotent side effects, and outbound rendering.
"""

from __future__ import annotations

import asyncio

from scripts.config import Settings
from services.coordination import (
    ADMITTED,
    DISCARDED,
    STALE_REVIEW_DISCARDED,
    DeterminismManifest,
    InMemoryManifestStore,
    InMemorySequenceGate,
    InMemorySideEffectLedger,
    InMemoryWorkClaimStore,
    SequencedAction,
    SideEffectRef,
    WorkUnit,
    partition_by_file_impact,
)
from services.integration.jira import (
    CLOUD,
    DATACENTER,
    IdempotentJiraWriter,
    SnapshotCandidate,
    TaskSnapshot,
    bullet_list,
    capture_task_snapshot,
    code,
    heading,
    link,
    paragraph,
    render,
)
from services.integration.jira.snapshot import snapshot_id_for

TASK_ID = "AGENTDEV-1"


def _dc_issue() -> dict:
    return {
        "key": TASK_ID,
        "fields": {
            "project": {"key": "AGENTDEV"},
            "summary": "Add retry to the client",
            "description": "Add retry with backoff.",
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "labels": ["backend", "resilience"],
        },
    }


def _settings() -> Settings:
    return Settings(allowlist_jira_projects="AGENTDEV", jira_acceptance_field="")


class _InMemorySnapshotStore:
    def __init__(self) -> None:
        self.by_execution: dict[str, list[TaskSnapshot]] = {}

    async def persist(self, candidate: SnapshotCandidate) -> TaskSnapshot:
        history = self.by_execution.setdefault(candidate.execution_id, [])
        if history and history[-1].content_hash == candidate.content_hash:
            return history[-1]
        version = history[-1].version + 1 if history else 1
        snapshot = TaskSnapshot(
            snapshot_id=snapshot_id_for(candidate.execution_id, version),
            execution_id=candidate.execution_id,
            task_id=candidate.task_id,
            jira_project=candidate.jira_project,
            jira_source=candidate.jira_source,
            version=version,
            content_hash=candidate.content_hash,
            payload=candidate.payload,
        )
        history.append(snapshot)
        return snapshot


class _FakeWriteClient:
    def __init__(self, status: str = "To Do") -> None:
        self.status = status
        self.comments: list[str] = []
        self.transitions: list[tuple[str, str]] = []

    def get_status(self, issue_key: str) -> str:
        return self.status

    def transition(self, issue_key: str, target_status: str) -> None:
        self.transitions.append((issue_key, target_status))
        self.status = target_status

    def list_comment_bodies(self, issue_key: str):
        return tuple(self.comments)

    def add_comment(self, issue_key: str, body: str) -> None:
        self.comments.append(body)


def _summary_document():
    return [
        heading("Execution summary"),
        paragraph("Opened ", link("PR #12", "https://ghes/pr/12"), " for the retry work."),
        bullet_list(["All checks passed", ["See ", link("build", "https://ci/build/9")]]),
        code("retry(max=3)", language="python"),
    ]


async def test_two_agents_run_non_colliding_units_with_full_coordination():
    settings = _settings()
    execution_id = "exec-1"

    snapshot_store = _InMemorySnapshotStore()
    snapshot = await capture_task_snapshot(
        _dc_issue(),
        execution_id=execution_id,
        jira_source=DATACENTER,
        settings=settings,
        store=snapshot_store,
    )
    assert snapshot.version == 1
    assert snapshot.jira_project == "AGENTDEV"
    assert snapshot.content_hash

    manifests = InMemoryManifestStore()
    await manifests.record(
        DeterminismManifest(
            execution_id=execution_id,
            task_id=TASK_ID,
            snapshot_version=snapshot.version,
            repository_sha="a1b2c3d4",
            policy_bundle_version="policy/v2",
            prompt_versions={"plan-review": "plan-review/v1"},
            resolved_models={"classification-summary": "bedrock/anthropic.claude-3"},
        )
    )
    assert len(manifests.events) == 1
    assert manifests.executions[execution_id]["repository_sha"] == "a1b2c3d4"

    unit_a = WorkUnit(TASK_ID, "agent-a", execution_id, "agent-a", ("src/client.py",))
    unit_b = WorkUnit(TASK_ID, "agent-b", execution_id, "agent-b", ("src/retry.py",))
    schedule = partition_by_file_impact([unit_a, unit_b])
    assert {u.work_unit for u in schedule.admitted} == {"agent-a", "agent-b"}
    assert schedule.deferred == ()

    claims = InMemoryWorkClaimStore()
    claim_a = await claims.try_claim(unit_a)
    claim_b = await claims.try_claim(unit_b)
    assert claim_a.won and claim_b.won

    contested = await asyncio.gather(claims.try_claim(unit_a), claims.try_claim(unit_a))
    assert [o.won for o in contested].count(True) == 0

    gate = InMemorySequenceGate()
    assert (await gate.admit(_action(execution_id, "agent-a", 1))).outcome == ADMITTED
    assert (await gate.admit(_action(execution_id, "agent-b", 2))).outcome == ADMITTED
    stale = await gate.admit(_action(execution_id, "agent-a", 1))
    assert stale.outcome == DISCARDED
    assert stale.receipt == STALE_REVIEW_DISCARDED
    assert len(gate.receipts) == 1
    assert (await gate.admit(_action(execution_id, "agent-b", 3))).outcome == ADMITTED

    ledger = InMemorySideEffectLedger()
    calls = {"n": 0}

    async def _post_effect():
        calls["n"] += 1
        return {"comment_id": "c1"}

    ref = SideEffectRef(TASK_ID, execution_id, "jira_comment", TASK_ID)
    first = await ledger.run_once(ref, _post_effect)
    replayed = await ledger.run_once(ref, _post_effect)
    assert first.replayed is False
    assert replayed.replayed is True
    assert replayed.result == first.result
    assert calls["n"] == 1

    writer = IdempotentJiraWriter(_FakeWriteClient())
    applied = writer.post_comment(TASK_ID, "PR ready", execution_id=execution_id, kind="pr_link")
    skipped = writer.post_comment(TASK_ID, "PR ready", execution_id=execution_id, kind="pr_link")
    assert applied.action != skipped.action

    document = _summary_document()
    wiki = render(document, DATACENTER)
    adf = render(document, CLOUD)
    assert isinstance(wiki, str)
    assert wiki.startswith("h2. Execution summary")
    assert "[PR #12|https://ghes/pr/12]" in wiki
    assert isinstance(adf, dict)
    assert adf["type"] == "doc"
    assert [node["type"] for node in adf["content"]] == [
        "heading",
        "paragraph",
        "bulletList",
        "codeBlock",
    ]


def _action(execution_id: str, agent_id: str, sequence: int) -> SequencedAction:
    return SequencedAction(
        task_id=TASK_ID,
        execution_id=execution_id,
        sequence=sequence,
        idempotency_key=f"{agent_id}:{sequence}",
        operation="jira_comment",
    )
