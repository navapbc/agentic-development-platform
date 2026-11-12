"""Edit-classification tests: material edits pause, metadata edits continue."""

from __future__ import annotations

import pytest
from services.integration.jira.edits import (
    EXECUTION_INVALIDATING,
    MATERIAL_CHANGE,
    METADATA_ONLY,
    NON_MATERIAL,
    UNCHANGED,
    PauseSignal,
    classify_edit,
    evaluate_task_edit,
    recheck_issue_before_pr,
)
from tests.support import hermetic_settings


class _RecordingPauseStore:
    def __init__(self) -> None:
        self.records: list = []

    async def record(self, record) -> None:
        self.records.append(record)


def _payload(**overrides):
    base = {
        "task_id": "AGENTDEV-1",
        "project": "AGENTDEV",
        "summary": "Add retry to the client",
        "description": "Add retry with backoff.",
        "issue_type": "Story",
        "status": "In Progress",
        "priority": "Medium",
        "labels": ["backend"],
        "acceptance_criteria": "",
        "comments": [],
        "components": [],
    }
    base.update(overrides)
    return base


def test_classify_unchanged():
    result = classify_edit(_payload(), _payload())
    assert result.category == UNCHANGED
    assert result.requires_pause is False


def test_classify_metadata_only():
    result = classify_edit(_payload(), _payload(priority="High", labels=["backend", "urgent"]))
    assert result.category == METADATA_ONLY
    assert result.requires_pause is False


def test_classify_comments_only_is_non_material():
    result = classify_edit(_payload(), _payload(comments=["please expedite"]))
    assert result.category == NON_MATERIAL
    assert result.requires_pause is False


def test_classify_material_requirements_change():
    result = classify_edit(_payload(), _payload(description="Add retry AND circuit breaker."))
    assert result.category == MATERIAL_CHANGE
    assert result.requires_pause is True
    assert "description" in result.changed_fields


def test_classify_execution_invalidating_on_issue_type():
    result = classify_edit(_payload(), _payload(issue_type="Bug"))
    assert result.category == EXECUTION_INVALIDATING
    assert result.requires_pause is True


def test_classify_execution_invalidating_on_cancel_status():
    result = classify_edit(_payload(), _payload(status="Cancelled"))
    assert result.category == EXECUTION_INVALIDATING
    assert result.requires_pause is True


async def test_evaluate_continue_returns_classification_without_recording():
    store = _RecordingPauseStore()
    result = await evaluate_task_edit(
        _payload(),
        _payload(priority="High"),
        execution_id="e1",
        task_id="AGENTDEV-1",
        store=store,
    )
    assert result.category == METADATA_ONLY
    assert store.records == []


async def test_evaluate_material_edit_records_and_raises_pause():
    store = _RecordingPauseStore()
    with pytest.raises(PauseSignal) as excinfo:
        await evaluate_task_edit(
            _payload(),
            _payload(summary="Completely different task"),
            execution_id="e1",
            task_id="AGENTDEV-1",
            store=store,
        )
    assert excinfo.value.classification.category == MATERIAL_CHANGE
    assert len(store.records) == 1
    assert store.records[0].execution_id == "e1"


class _FakeReader:
    def __init__(self, raw: dict) -> None:
        self._raw = raw

    def get_issue(self, issue_key: str) -> dict:
        return self._raw

    def recheck_payload(self, issue_key: str, *, raw_comments: tuple = ()) -> dict:
        from services.integration.jira.normalize import normalize_issue

        return normalize_issue(self._raw, raw_comments=raw_comments, acceptance_field="").to_dict()


def _raw_issue(summary: str = "Add retry to the client") -> dict:
    return {
        "key": "AGENTDEV-1",
        "fields": {
            "project": {"key": "AGENTDEV"},
            "summary": summary,
            "description": "Add retry with backoff.",
            "issuetype": {"name": "Story"},
            "status": {"name": "In Progress"},
            "priority": {"name": "Medium"},
            "labels": ["backend"],
        },
    }


async def test_recheck_before_pr_pauses_on_material_edit():
    settings = hermetic_settings()
    store = _RecordingPauseStore()
    stored = _payload()
    reader = _FakeReader(_raw_issue(summary="Rewrite the whole client"))

    with pytest.raises(PauseSignal):
        await recheck_issue_before_pr(
            reader,
            "AGENTDEV-1",
            stored,
            execution_id="e1",
            task_id="AGENTDEV-1",
            settings=settings,
            store=store,
        )
    assert len(store.records) == 1


async def test_recheck_before_pr_continues_when_unchanged():
    settings = hermetic_settings()
    store = _RecordingPauseStore()
    stored = _payload()
    reader = _FakeReader(_raw_issue())

    result = await recheck_issue_before_pr(
        reader,
        "AGENTDEV-1",
        stored,
        execution_id="e1",
        task_id="AGENTDEV-1",
        settings=settings,
        store=store,
    )
    assert result.category == UNCHANGED
    assert store.records == []
