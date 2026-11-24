"""Each tenant's runs use their own task queue, which the workflow worker also polls."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from services.orchestration.task_queues import (
    TENANT_NAME,
    review_task_queue,
    run_review_task_queue,
    run_task_queue,
    tenant_schedule_id,
    workflow_task_queues,
)
from tests.support import hermetic_settings

TENANT_VARIABLES = (
    Path(__file__).resolve().parents[2] / "infra" / "terraform" / "tenant" / "variables.tf"
)


def test_a_tenant_runs_on_its_own_queue() -> None:
    settings = hermetic_settings(temporal_task_queue="agent-platform", tenant="adp")

    assert run_task_queue(settings) == "agent-platform-adp"
    assert tenant_schedule_id("orphan-reaper", settings) == "orphan-reaper-adp"


def test_a_process_without_a_tenant_uses_the_base_queue() -> None:
    settings = hermetic_settings(temporal_task_queue="agent-platform", tenant="")

    assert run_task_queue(settings) == "agent-platform"
    assert tenant_schedule_id("orphan-reaper", settings) == "orphan-reaper"


def test_a_tenant_reviews_on_its_own_review_queue() -> None:
    settings = hermetic_settings(review_task_queue="agent-platform-review", tenant="beta")

    assert review_task_queue(settings, "adp") == "agent-platform-review-adp"
    assert run_review_task_queue(settings) == "agent-platform-review-beta"
    assert run_review_task_queue(settings.model_copy(update={"tenant": ""})) == (
        "agent-platform-review"
    )


def test_the_workflow_worker_polls_the_base_queue_and_every_tenant_queue() -> None:
    settings = hermetic_settings(temporal_task_queue="agent-platform", tenants="adp, payments")

    assert workflow_task_queues(settings) == [
        "agent-platform",
        "agent-platform-adp",
        "agent-platform-payments",
    ]


@pytest.mark.parametrize(
    "name",
    ["adp", "a1", "payments2026", "abcdefghijklmnop", "abcdefghijklmnopq", "1abc", "default"],
)
def test_the_tenant_name_pattern_accepts_what_the_terraform_tenant_validation_accepts(
    name: str,
) -> None:
    condition = re.search(
        r'variable "tenant" \{.*?condition\s*=\s*([^\n]+)', TENANT_VARIABLES.read_text(), re.DOTALL
    )
    assert condition is not None
    validation = re.fullmatch(
        r'can\(regex\("([^"]+)", var\.tenant\)\) && var\.tenant != "([a-z]+)"',
        condition.group(1).strip(),
    )
    assert validation is not None
    pattern, reserved = validation.groups()
    terraform_accepts = re.fullmatch(pattern, name) is not None and name != reserved

    assert (TENANT_NAME.fullmatch(name) is not None) == terraform_accepts
