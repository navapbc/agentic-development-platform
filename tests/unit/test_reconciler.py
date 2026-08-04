"""Unit tests for the suspension registry store twin and the resume reconciler."""

from __future__ import annotations

import pytest
from services.orchestration.reconciler import (
    ResumePlan,
    plan_build_resumes,
    reconcile,
)
from services.orchestration.suspension import ResumeOn
from services.orchestration.suspension_store import (
    InMemorySuspensionStore,
    SuspensionRecord,
)


@pytest.mark.asyncio
async def test_in_memory_store_record_list_clear_roundtrip():
    store = InMemorySuspensionStore()
    record = SuspensionRecord(
        workflow_id="wf-1",
        reason="awaiting_fix",
        resume_on=ResumeOn.NEWER_BUILD.value,
        parked_build_id="1.4.1",
        stage="finalize",
        detail="boom",
    )
    await store.record(record)
    assert await store.list_open() == [record]

    refreshed = SuspensionRecord(
        workflow_id="wf-1",
        reason="awaiting_fix",
        resume_on=ResumeOn.NEWER_BUILD.value,
        parked_build_id="1.4.2",
    )
    await store.record(refreshed)
    rows = await store.list_open()
    assert len(rows) == 1
    assert rows[0].parked_build_id == "1.4.2"

    await store.clear("wf-1")
    assert await store.list_open() == []


def test_plan_build_resumes_only_targets_older_build_newer_build_reason():
    records = [
        SuspensionRecord("wf-old", "awaiting_fix", ResumeOn.NEWER_BUILD.value, "1.4.1"),
        SuspensionRecord("wf-same", "awaiting_fix", ResumeOn.NEWER_BUILD.value, "1.4.2"),
        SuspensionRecord("wf-review", "awaiting_review", ResumeOn.HUMAN_APPROVAL.value, "1.4.1"),
    ]
    plans = plan_build_resumes(records, current_build_id="1.4.2")
    assert plans == [ResumePlan(workflow_id="wf-old", trigger=ResumeOn.NEWER_BUILD)]


@pytest.mark.asyncio
async def test_reconcile_signals_each_planned_workflow():
    store = InMemorySuspensionStore()
    await store.record(
        SuspensionRecord("wf-old", "awaiting_fix", ResumeOn.NEWER_BUILD.value, "1.4.1")
    )
    await store.record(
        SuspensionRecord("wf-review", "awaiting_review", ResumeOn.HUMAN_APPROVAL.value, "1.4.1")
    )
    sent: list[tuple[str, ResumeOn]] = []

    async def send_resume(workflow_id: str, trigger: ResumeOn) -> None:
        sent.append((workflow_id, trigger))

    plans = await reconcile(store, send_resume, current_build_id="1.4.2")
    assert sent == [("wf-old", ResumeOn.NEWER_BUILD)]
    assert [p.workflow_id for p in plans] == ["wf-old"]
