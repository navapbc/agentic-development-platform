"""The remedy and alert-channel fields survive the Temporal payload boundary.

A worker running an older build drops unknown keys rather than failing, and a terminal reason the
receiving worker has never seen must decode rather than raise, because a converter ``TypeError``
fails the workflow task and retries without bound.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from services.orchestration.temporal_activities.notify import AlertHumanInterventionRequest
from services.orchestration.temporal_activities.persistence import AuditEvent
from temporalio.converter import DataConverter

CONVERTER = DataConverter.default


async def _round_trip(value, target):
    payloads = await CONVERTER.encode([value])
    decoded = await CONVERTER.decode(payloads, [target])
    return decoded[0]


@pytest.mark.asyncio
async def test_alert_request_carries_the_remedy_across_the_boundary():
    request = AlertHumanInterventionRequest(
        task_id="T-1",
        execution_id="exec-1",
        workflow_id="wf-1",
        repo="navapbc/agentic-demo-service",
        issue_key="ADSER-24",
        reason="repair_exhausted",
        stage="repair",
        detail="pytest failed",
        remedy="Run `inv repair --issue ADSER-24 --retry`.",
    )

    assert await _round_trip(request, AlertHumanInterventionRequest) == request


@pytest.mark.asyncio
async def test_audit_event_carries_the_remedy_and_channels_across_the_boundary():
    event = AuditEvent(
        event_id="exec-1:HUMAN_INTERVENTION_ALERTED:repair:1",
        task_id="T-1",
        execution_id="exec-1",
        event_type="HUMAN_INTERVENTION_ALERTED",
        stage="repair",
        reason="repair_exhausted",
        remedy="Run `inv repair --issue ADSER-24 --retry`.",
        alert_channels=["jira", "slack"],
    )

    assert await _round_trip(event, AuditEvent) == event


@dataclass
class _OlderAlertRequest:
    task_id: str
    execution_id: str
    workflow_id: str
    repo: str
    issue_key: str
    reason: str
    stage: str
    detail: str


@pytest.mark.asyncio
async def test_a_worker_without_the_remedy_field_decodes_the_request_anyway():
    request = AlertHumanInterventionRequest(
        task_id="T-1",
        execution_id="exec-1",
        workflow_id="wf-1",
        repo="navapbc/agentic-demo-service",
        issue_key="ADSER-24",
        reason="repair_exhausted",
        stage="repair",
        detail="pytest failed",
        remedy="Run `inv repair --issue ADSER-24 --retry`.",
    )

    older = await _round_trip(request, _OlderAlertRequest)

    assert older.reason == "repair_exhausted"


@pytest.mark.asyncio
async def test_a_terminal_reason_the_receiver_does_not_know_decodes_rather_than_raises():
    request = AlertHumanInterventionRequest(
        task_id="T-1",
        execution_id="exec-1",
        workflow_id="wf-1",
        repo="navapbc/agentic-demo-service",
        issue_key="ADSER-24",
        reason="a_token_no_worker_knows",
        stage="repair",
        detail="",
        remedy="",
    )

    decoded = await _round_trip(request, AlertHumanInterventionRequest)

    assert decoded.reason == "a_token_no_worker_knows"
