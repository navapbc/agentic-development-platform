"""Live Slack checks for the release verification runbook, skipped unless explicitly enabled.

Slack offers no API that presses a message button as a user, so a person clicks during the
runbook and these tests assert what the click changed. Each test waits for the click.

Set RUN_LIVE_SLACK=1 and LIVE_SLACK_EXECUTION_ID to the execution parked at a human gate whose
Slack message is in the tenant's channel. Then run one test at a time with ``-k``:

- ``approved_from_slack``: an allowlisted user clicks Approve. The test asserts the decision audit
  event names that user and the execution leaves AWAITING_HUMAN.
- ``outside_the_allowlist``: a user outside the allowlist clicks a button on a parked run's
  message. The test asserts the refusal audit event.

LIVE_SLACK_WAIT_SECONDS bounds each wait and defaults to 300.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from database.engine import create_platform_engine
from database.models.schema import audit_events, executions
from scripts.config import get_settings, to_host_url
from sqlalchemy import select

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SLACK") != "1" or not os.environ.get("LIVE_SLACK_EXECUTION_ID"),
    reason="set RUN_LIVE_SLACK=1 and LIVE_SLACK_EXECUTION_ID to run the live Slack checks",
)

EXECUTION_ID = os.environ.get("LIVE_SLACK_EXECUTION_ID", "")
WAIT_SECONDS = float(os.environ.get("LIVE_SLACK_WAIT_SECONDS", "300"))


async def _wait_for(query, describe: str):
    engine = create_platform_engine(to_host_url(get_settings().database_url))
    deadline = time.monotonic() + WAIT_SECONDS
    try:
        while time.monotonic() < deadline:
            async with engine.connect() as conn:
                row = (await conn.execute(query)).first()
            if row is not None:
                return row
            await asyncio.sleep(2)
    finally:
        await engine.dispose()
    pytest.fail(f"no {describe} for {EXECUTION_ID} within {WAIT_SECONDS:.0f} seconds")


async def test_a_gate_park_is_approved_from_slack():
    decision = await _wait_for(
        select(audit_events.c.decision, audit_events.c.actor_type, audit_events.c.actor_id).where(
            audit_events.c.execution_id == EXECUTION_ID,
            audit_events.c.event_type == "HUMAN_INTERVENTION_DECISION",
            audit_events.c.actor_type == "slack",
        ),
        "decision from Slack",
    )

    assert decision.decision == "approved"
    assert decision.actor_id.startswith("slack:")
    await _wait_for(
        select(executions.c.state).where(
            executions.c.execution_id == EXECUTION_ID, executions.c.state != "AWAITING_HUMAN"
        ),
        "resumed execution",
    )


async def test_a_click_from_outside_the_allowlist_is_refused_and_audited():
    refusal = await _wait_for(
        select(audit_events.c.reason, audit_events.c.actor_id).where(
            audit_events.c.execution_id == EXECUTION_ID,
            audit_events.c.event_type == "SLACK_ACTION_REFUSED",
            audit_events.c.reason == "not_allowed",
        ),
        "refusal of a click from outside the allowlist",
    )

    assert refusal.actor_id.startswith("slack:")
