"""Temporal worker entry point."""

from __future__ import annotations

import asyncio
import logging
import os

from observability import configure_tracing, temporal_interceptors
from scripts.config import get_settings
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from services.orchestration.temporal_activities import (
    alert_human_intervention,
    analyze_ci_failure,
    apply_ci_repair,
    apply_task_status,
    claim_work_unit,
    classify_test_failures,
    clear_suspension,
    commit_and_push,
    count_tick,
    create_draft_pr,
    evaluate_gate,
    evaluate_promotion,
    find_open_pr,
    gather_repo_context,
    invoke_model,
    link_pr_to_issue,
    load_gate_plan,
    mark_task_parked,
    persist_execution_state,
    poll_ci,
    promote_pr,
    read_ci_log,
    reap_orphans,
    recheck_issue,
    reconcile_intake_pass,
    record_audit_event,
    record_manifest,
    record_pr_approval,
    record_suspension,
    record_usage_metrics,
    resolve_conflicts,
    review_code,
    review_plan,
    review_security,
    run_implementation_stage,
    scan_change,
    synchronize_branch,
    trigger_ci,
    unmark_task_parked,
    verify_acceptance,
)
from services.orchestration.temporal_workflows import (
    BranchSyncWorkflow,
    CountingWorkflow,
    DraftPullRequestWorkflow,
    DurableTaskWorkflow,
    ImplementationWorkflow,
    IntakeWorkflow,
    PlanReviewWorkflow,
    PullRequestWorkflow,
    ReapWorkflow,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("workflow-worker")

WORKER_ROLE_ALL = "all"
WORKER_ROLE_WORKFLOW = "workflow"
WORKER_ROLE_ACTIVITY = "activity"
_WORKER_ROLES = frozenset({WORKER_ROLE_ALL, WORKER_ROLE_WORKFLOW, WORKER_ROLE_ACTIVITY})

_WORKFLOWS = [
    DurableTaskWorkflow,
    CountingWorkflow,
    PlanReviewWorkflow,
    ImplementationWorkflow,
    BranchSyncWorkflow,
    PullRequestWorkflow,
    DraftPullRequestWorkflow,
    ReapWorkflow,
    IntakeWorkflow,
]

_ACTIVITIES = [
    invoke_model,
    persist_execution_state,
    record_audit_event,
    record_usage_metrics,
    gather_repo_context,
    count_tick,
    review_plan,
    review_code,
    review_security,
    verify_acceptance,
    claim_work_unit,
    run_implementation_stage,
    synchronize_branch,
    resolve_conflicts,
    recheck_issue,
    find_open_pr,
    commit_and_push,
    create_draft_pr,
    link_pr_to_issue,
    mark_task_parked,
    unmark_task_parked,
    apply_task_status,
    record_pr_approval,
    promote_pr,
    reap_orphans,
    record_suspension,
    clear_suspension,
    trigger_ci,
    poll_ci,
    read_ci_log,
    analyze_ci_failure,
    apply_ci_repair,
    alert_human_intervention,
    scan_change,
    evaluate_promotion,
    evaluate_gate,
    load_gate_plan,
    classify_test_failures,
    record_manifest,
    reconcile_intake_pass,
]


def _resolve_role() -> str:
    role = os.environ.get("WORKER_ROLE", WORKER_ROLE_ALL).strip().lower()
    if role not in _WORKER_ROLES:
        raise SystemExit(f"invalid WORKER_ROLE {role!r}; expected one of {sorted(_WORKER_ROLES)}")
    return role


def build_worker(client: Client, task_queue: str, role: str) -> Worker:
    runner = SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "beartype", "opentelemetry"
        )
    )
    settings = get_settings()
    versioning: dict[str, object] = {}
    if settings.worker_versioning_enabled:
        versioning = {
            "build_id": settings.agent_build_id,
            "use_worker_versioning": True,
        }
    if role == WORKER_ROLE_WORKFLOW:
        return Worker(
            client,
            task_queue=task_queue,
            workflows=_WORKFLOWS,
            workflow_runner=runner,
            **versioning,
        )
    if role == WORKER_ROLE_ACTIVITY:
        return Worker(client, task_queue=task_queue, activities=_ACTIVITIES, **versioning)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=_WORKFLOWS,
        activities=_ACTIVITIES,
        workflow_runner=runner,
        **versioning,
    )


async def main() -> None:
    settings = get_settings()
    role = _resolve_role()
    configure_tracing(f"workflow-worker.{role}")
    log.info(
        "connecting to Temporal at %s (ns=%s)",
        settings.temporal_host,
        settings.temporal_namespace,
    )
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
        interceptors=temporal_interceptors(),
    )

    worker = build_worker(client, settings.temporal_task_queue, role)
    log.info(
        "worker started on task queue '%s' (role=%s, build=%s)",
        settings.temporal_task_queue,
        role,
        settings.agent_build_id,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
