"""Unit test for the invoke task surface."""

from __future__ import annotations

import tasks as tasks_module
from invoke import Collection

EXPECTED_TASKS = {
    "tunnel-start",
    "tunnel-stop",
    "tunnel-status",
    "connectivity-check",
    "config-check",
    "local-up",
    "local-down",
    "local-stop",
    "local-destroy",
    "logs",
    "db-migrate",
    "db-reset",
    "seed",
    "smoke-test",
    "prompts-seed",
    "review-eval",
    "context-eval",
    "test",
    "workflow-status",
    "durability-proof",
    "fetch-acli",
    "cbm-build",
    "reap",
    "intake",
    "repair",
    "gate",
    "restart",
    "demo-durable-task",
    "demo-durable-counter",
    "demo-coordination",
    "demo-repo-intelligence",
    "demo-draft-pr",
    "approve-pr",
    "k8s-deploy",
    "k8s-delete",
    "webhook-setup",
    "webhook-teardown",
}


def test_invoke_namespace_exposes_documented_tasks():
    ns = tasks_module.ns
    assert isinstance(ns, Collection)
    assert set(ns.task_names) == EXPECTED_TASKS
