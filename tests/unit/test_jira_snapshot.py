import pytest
from scripts.config import Settings
from services.integration.jira import (
    CLOUD,
    DATACENTER,
    AllowlistError,
    SnapshotCandidate,
    TaskSnapshot,
    capture_task_snapshot,
    content_hash,
    normalize_issue,
)
from services.integration.jira.snapshot import snapshot_id_for
from tests.support import hermetic_settings


@pytest.fixture(autouse=True)
def _allowlist_from_config(monkeypatch):
    monkeypatch.setattr(
        "services.integration.jira.snapshot.allowed_projects",
        lambda settings=None: {"AGENTDEV"},
    )
    monkeypatch.setattr(
        "services.integration.jira.snapshot.acceptance_field",
        lambda settings=None: "",
    )


def _dc_issue() -> dict:
    return {
        "key": "AGENTDEV-1",
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


def _cloud_issue() -> dict:
    return {
        "key": "AGENTDEV-1",
        "fields": {
            "project": {"key": "AGENTDEV"},
            "summary": "Add retry to the client",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Add retry with backoff."}],
                    }
                ],
            },
            "issuetype": {"name": "Story"},
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "labels": ["backend", "resilience"],
        },
    }


class _RecordingStore:
    def __init__(self) -> None:
        self.by_execution: dict[str, list[TaskSnapshot]] = {}
        self.audits: list[str] = []

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
        self.audits.append(snapshot.snapshot_id)
        return snapshot


def _settings() -> Settings:
    return hermetic_settings()


def test_normalize_datacenter_issue():
    task = normalize_issue(_dc_issue())
    assert task.task_id == "AGENTDEV-1"
    assert task.project == "AGENTDEV"
    assert task.summary == "Add retry to the client"
    assert "backoff" in task.description
    assert task.issue_type == "Story"
    assert task.status == "To Do"
    assert task.priority == "High"
    assert task.labels == ("backend", "resilience")


def test_normalize_cloud_adf_description_to_text():
    task = normalize_issue(_cloud_issue())
    assert task.description == "Add retry with backoff."


def test_normalize_comments_and_acceptance_field():
    issue = _dc_issue()
    issue["fields"]["customfield_100"] = "Given X when Y then Z"
    task = normalize_issue(
        issue,
        raw_comments=("first note", "second note"),
        acceptance_field="customfield_100",
    )
    assert task.acceptance_criteria == "Given X when Y then Z"
    assert task.comments == ("first note", "second note")


def test_content_hash_is_stable_and_content_sensitive():
    a = normalize_issue(_dc_issue()).to_dict()
    b = normalize_issue(_cloud_issue()).to_dict()
    assert content_hash(a) == content_hash(b)
    changed = normalize_issue(_dc_issue()).to_dict()
    changed["summary"] = "different"
    assert content_hash(changed) != content_hash(a)


async def test_capture_persists_versioned_snapshot_and_audit():
    store = _RecordingStore()
    snapshot = await capture_task_snapshot(
        _dc_issue(),
        execution_id="exec-1",
        jira_source=DATACENTER,
        settings=_settings(),
        store=store,
    )
    assert snapshot.version == 1
    assert snapshot.jira_source == DATACENTER
    assert snapshot.jira_project == "AGENTDEV"
    assert snapshot.content_hash
    assert store.audits == [snapshot.snapshot_id]


async def test_recapture_same_content_is_idempotent():
    store = _RecordingStore()
    first = await capture_task_snapshot(
        _dc_issue(),
        execution_id="exec-1",
        jira_source=DATACENTER,
        settings=_settings(),
        store=store,
    )
    second = await capture_task_snapshot(
        _cloud_issue(), execution_id="exec-1", jira_source=CLOUD, settings=_settings(), store=store
    )
    assert second.snapshot_id == first.snapshot_id
    assert second.version == 1
    assert len(store.by_execution["exec-1"]) == 1


async def test_recapture_changed_content_bumps_version():
    store = _RecordingStore()
    await capture_task_snapshot(
        _dc_issue(),
        execution_id="exec-1",
        jira_source=DATACENTER,
        settings=_settings(),
        store=store,
    )
    changed = _dc_issue()
    changed["fields"]["summary"] = "Add retry and jitter"
    second = await capture_task_snapshot(
        changed, execution_id="exec-1", jira_source=DATACENTER, settings=_settings(), store=store
    )
    assert second.version == 2
    assert len(store.by_execution["exec-1"]) == 2


async def test_capture_rejects_non_allowlisted_project():
    store = _RecordingStore()
    issue = _dc_issue()
    issue["key"] = "OTHER-9"
    issue["fields"]["project"] = {"key": "OTHER"}
    with pytest.raises(AllowlistError):
        await capture_task_snapshot(
            issue, execution_id="exec-1", jira_source=DATACENTER, settings=_settings(), store=store
        )
    assert store.by_execution == {}
