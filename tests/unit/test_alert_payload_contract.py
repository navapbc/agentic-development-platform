"""The remedy and alert-channel fields survive the Temporal payload boundary.

A worker running an older build drops unknown keys rather than failing, and a terminal reason or
remedy step the receiving worker has never seen must decode rather than raise, because a
converter ``TypeError`` fails the workflow task and retries without bound.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from services.operator_remedy import (
    DeploymentPosture,
    Remedy,
    RemedyStep,
    remedy,
    render,
    retry_repair,
)
from services.orchestration.temporal_activities.notify import (
    AlertHumanInterventionRequest,
    AlertHumanInterventionResult,
)
from services.orchestration.temporal_activities.persistence import AuditEvent
from temporalio.converter import DataConverter

CONVERTER = DataConverter.default


async def _round_trip(value, target):
    payloads = await CONVERTER.encode([value])
    decoded = await CONVERTER.decode(payloads, [target])
    return decoded[0]


def _request(**overrides) -> AlertHumanInterventionRequest:
    fields = {
        "task_id": "T-1",
        "execution_id": "exec-1",
        "workflow_id": "wf-1",
        "repo": "navapbc/agentic-demo-service",
        "issue_key": "ADSER-24",
        "reason": "repair_exhausted",
        "stage": "repair",
        "detail": "pytest failed",
        "remedy": remedy("Run ", retry_repair("ADSER-24"), "."),
    }
    return AlertHumanInterventionRequest(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_alert_request_carries_the_structured_remedy_across_the_boundary():
    request = _request()

    assert await _round_trip(request, AlertHumanInterventionRequest) == request


@pytest.mark.asyncio
async def test_alert_result_carries_the_rendered_remedy_and_channels_back():
    result = AlertHumanInterventionResult(
        channels=["jira", "slack"], remedy="Run `inv repair --issue ADSER-24 --retry`."
    )

    assert await _round_trip(result, AlertHumanInterventionResult) == result


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


@pytest.mark.asyncio
async def test_the_park_key_and_decider_cross_the_boundary():
    request = _request(park_key="exec-1:repair:1:1")
    event = AuditEvent(
        event_id="exec-1:HUMAN_INTERVENTION_DECISION:1",
        task_id="T-1",
        execution_id="exec-1",
        event_type="HUMAN_INTERVENTION_DECISION",
        stage="repair",
        decision="retry",
        actor_type="slack",
        actor_id="slack:E1/U1",
        correlation_ids={"park_key": "exec-1:repair:1:1"},
    )

    assert await _round_trip(request, AlertHumanInterventionRequest) == request
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
    older = await _round_trip(_request(), _OlderAlertRequest)

    assert older.reason == "repair_exhausted"


@pytest.mark.asyncio
async def test_a_terminal_reason_the_receiver_does_not_know_decodes_rather_than_raises():
    request = _request(reason="a_token_no_worker_knows", remedy=Remedy())

    decoded = await _round_trip(request, AlertHumanInterventionRequest)

    assert decoded.reason == "a_token_no_worker_knows"


@pytest.mark.asyncio
async def test_a_remedy_step_the_receiver_does_not_know_renders_as_its_text():
    request = _request(
        remedy=Remedy((RemedyStep("a_step_no_worker_knows", text="Ask the platform team."),))
    )

    decoded = await _round_trip(request, AlertHumanInterventionRequest)

    assert render(decoded.remedy, DeploymentPosture.HOST) == ("Ask the platform team.")


@pytest.mark.asyncio
async def test_the_alert_activity_hands_the_park_and_its_deadline_to_the_sender(monkeypatch):
    import time

    from services.orchestration.temporal_activities.notify import alert_human_intervention
    from temporalio.testing import ActivityEnvironment

    sent: list[tuple] = []

    def fake_send(alert, settings, *, issued_at, deadline, posts=None):
        sent.append((alert, issued_at, deadline))
        return ["slack"]

    monkeypatch.setattr(
        "services.orchestration.notifications.send_human_intervention_alert", fake_send
    )
    _in_memory_alert_ledger(monkeypatch)
    request = _request(park_key="exec-1:repair:1:1")

    environment = ActivityEnvironment()
    scheduled = datetime.now(UTC) - timedelta(seconds=5)
    environment.info = replace(
        environment.info,
        scheduled_time=scheduled,
        started_time=datetime.now(UTC),
        start_to_close_timeout=timedelta(seconds=30),
    )

    result = await environment.run(alert_human_intervention, request)

    [(alert, issued_at, deadline)] = sent
    assert (alert.park_key, alert.structured_remedy) == (request.park_key, request.remedy)
    assert issued_at == int(scheduled.timestamp())
    assert 25 < deadline - time.monotonic() <= 30
    assert result.channels == ["slack"]


def _in_memory_alert_ledger(monkeypatch):
    from services.coordination.idempotency import InMemorySideEffectLedger

    ledger = InMemorySideEffectLedger()
    monkeypatch.setattr(
        "services.orchestration.temporal_activities.notify._alert_ledger", lambda: ledger
    )
    return ledger


@pytest.mark.asyncio
async def test_a_retried_alert_attempt_replays_the_delivery_instead_of_posting_again(monkeypatch):
    from services.orchestration.temporal_activities.notify import alert_human_intervention
    from temporalio.testing import ActivityEnvironment

    sent: list[str] = []

    def fake_send(alert, settings, *, issued_at, deadline, posts=None):
        sent.append(alert.park_key)
        return ["jira", "slack"]

    monkeypatch.setattr(
        "services.orchestration.notifications.send_human_intervention_alert", fake_send
    )
    _in_memory_alert_ledger(monkeypatch)
    request = _request(park_key="exec-1:repair:1:1")
    first, retry = ActivityEnvironment(), ActivityEnvironment()
    retry.info = replace(first.info, attempt=2)

    delivered = await first.run(alert_human_intervention, request)
    replayed = await retry.run(alert_human_intervention, request)

    assert sent == ["exec-1:repair:1:1"]
    assert delivered.channels == replayed.channels == ["jira", "slack"]


@pytest.mark.asyncio
async def test_an_alert_attempt_backs_off_while_an_earlier_attempt_is_delivering(monkeypatch):
    from services.coordination.idempotency import CLAIM_LEASE_SECONDS, SideEffectOutcome
    from services.orchestration.temporal_activities.notify import alert_human_intervention
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import ActivityEnvironment

    class _DeliveringLedger:
        async def run_once(self, ref, effect):
            return SideEffectOutcome("key", replayed=False, result=None, in_flight=True)

    sent: list[str] = []
    monkeypatch.setattr(
        "services.orchestration.notifications.send_human_intervention_alert",
        lambda alert, settings, **_: sent.append(alert.park_key) or ["slack"],
    )
    monkeypatch.setattr(
        "services.orchestration.temporal_activities.notify._alert_ledger", _DeliveringLedger
    )

    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            alert_human_intervention, _request(park_key="exec-1:repair:1:1")
        )

    assert sent == []
    assert excinfo.value.next_retry_delay == timedelta(seconds=CLAIM_LEASE_SECONDS + 5)
