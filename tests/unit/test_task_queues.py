"""Each tenant's runs use their own task queue, which the workflow worker also polls."""

from __future__ import annotations

from services.orchestration.task_queues import (
    run_task_queue,
    tenant_schedule_id,
    workflow_task_queues,
)
from tests.support import hermetic_settings


def test_a_tenant_runs_on_its_own_queue() -> None:
    settings = hermetic_settings(temporal_task_queue="agent-platform", tenant="adp")

    assert run_task_queue(settings) == "agent-platform-adp"
    assert tenant_schedule_id("orphan-reaper", settings) == "orphan-reaper-adp"


def test_a_process_without_a_tenant_uses_the_base_queue() -> None:
    settings = hermetic_settings(temporal_task_queue="agent-platform", tenant="")

    assert run_task_queue(settings) == "agent-platform"
    assert tenant_schedule_id("orphan-reaper", settings) == "orphan-reaper"


def test_the_workflow_worker_polls_the_base_queue_and_every_tenant_queue() -> None:
    settings = hermetic_settings(temporal_task_queue="agent-platform", tenants="adp, payments")

    assert workflow_task_queues(settings) == [
        "agent-platform",
        "agent-platform-adp",
        "agent-platform-payments",
    ]
