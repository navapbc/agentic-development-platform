"""Tests for the draft PR creation and Jira linkback workflow stage."""

from __future__ import annotations

import uuid

from services.orchestration.temporal_activities.persistence import AuditEvent, ExecutionState
from services.orchestration.temporal_activities.pull_request import (
    LinkbackRequest,
    LinkbackResult,
    OpenPrRequest,
    OpenPrResult,
)
from services.orchestration.temporal_workflows.pull_request import (
    PullRequestInput,
    PullRequestWorkflow,
)
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


async def _run_workflow(
    *,
    created: bool = True,
    linkback_action: str = "applied",
    issue_key: str = "AGENTDEV-1",
):
    states: list[str] = []
    audits: list[AuditEvent] = []
    open_calls: list[OpenPrRequest] = []
    link_calls: list[LinkbackRequest] = []

    @activity.defn(name="persist_execution_state")
    async def fake_persist(payload: ExecutionState) -> str:
        states.append(payload.state)
        return payload.execution_id

    @activity.defn(name="record_audit_event")
    async def fake_audit(event: AuditEvent) -> str:
        audits.append(event)
        return event.event_id

    @activity.defn(name="create_draft_pr")
    async def fake_open(req: OpenPrRequest) -> OpenPrResult:
        open_calls.append(req)
        return OpenPrResult(
            number=7,
            url="https://ghes/acme/svc/pull/7",
            created=created,
            draft=True,
            state="open",
            orphan_id="orphan-7",
        )

    @activity.defn(name="link_pr_to_issue")
    async def fake_link(req: LinkbackRequest) -> LinkbackResult:
        link_calls.append(req)
        return LinkbackResult(action=linkback_action, detail="ok")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"pr-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[PullRequestWorkflow],
            activities=[fake_persist, fake_audit, fake_open, fake_link],
        ):
            result = await env.client.execute_workflow(
                PullRequestWorkflow.run,
                PullRequestInput(
                    task_id="AGENTDEV-1",
                    execution_id="exec-1",
                    repo="agentic-demo-service",
                    base="main",
                    branch="agent/AGENTDEV-1/exec-1",
                    title="Draft change",
                    body="body",
                    issue_key=issue_key,
                    base_sha="a" * 40,
                    sequence=3,
                ),
                id=f"wf-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    return result, states, audits, open_calls, link_calls


def _event_types(audits: list[AuditEvent]) -> list[str]:
    return [a.event_type for a in audits]


async def test_opens_draft_pr_and_links_back_to_jira():
    result, states, audits, open_calls, link_calls = await _run_workflow()

    assert states == ["PR_CREATING", "PR_CREATED"]
    assert result.number == 7
    assert result.url == "https://ghes/acme/svc/pull/7"
    assert result.created is True
    assert result.linkback == "applied"
    assert result.orphan_id == "orphan-7"
    assert len(open_calls) == 1
    assert open_calls[0].sequence == 3
    assert len(link_calls) == 1
    assert link_calls[0].pr_url == "https://ghes/acme/svc/pull/7"
    types = _event_types(audits)
    assert types == ["PR_CREATION_AUTHORIZED", "PR_CREATED", "PR_LINKED"]
    created_event = next(a for a in audits if a.event_type == "PR_CREATED")
    assert created_event.decision == "created"
    assert created_event.artifact_hashes["pr_url"] == "https://ghes/acme/svc/pull/7"


async def test_updated_pr_records_updated_decision():
    result, _, audits, _, _ = await _run_workflow(created=False)

    assert result.created is False
    created_event = next(a for a in audits if a.event_type == "PR_CREATED")
    assert created_event.decision == "updated"


async def test_skips_linkback_when_no_issue_key():
    result, _, audits, _, link_calls = await _run_workflow(issue_key="")

    assert result.linkback == "none"
    assert link_calls == []
    assert "PR_LINKED" not in _event_types(audits)


async def test_idempotent_linkback_surfaces_skipped_action():
    result, _, audits, _, _ = await _run_workflow(linkback_action="skipped")

    assert result.linkback == "skipped"
    linked = next(a for a in audits if a.event_type == "PR_LINKED")
    assert linked.decision == "skipped"
