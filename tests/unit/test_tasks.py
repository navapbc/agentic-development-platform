"""Unit test for the invoke task surface."""

from __future__ import annotations

import tasks as tasks_module
from invoke import Collection

EXPECTED_TASKS = {
    "tunnel-start",
    "tunnel-stop",
    "tunnel-status",
    "connectivity-check",
    "local-up",
    "local-down",
    "logs",
    "db-migrate",
    "db-reset",
    "seed",
    "smoke-test",
    "test",
    "workflow-status",
    "durability-proof",
    "fetch-acli",
    "reap",
    "demo-durable-task",
    "demo-durable-counter",
    "demo-coordination",
    "demo-repo-intelligence",
    "demo-draft-pr",
}


def test_invoke_namespace_exposes_documented_tasks():
    ns = tasks_module.ns
    assert isinstance(ns, Collection)
    assert set(ns.task_names) == EXPECTED_TASKS
