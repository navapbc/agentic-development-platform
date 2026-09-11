"""Temporal worker entry point."""

from __future__ import annotations

import asyncio
import logging
import os

from observability import (
    configure_logging,
    configure_metrics,
    configure_tracing,
    install_litellm_header_propagation,
    temporal_interceptors,
)
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
    promote_pr,
    read_ticket_status,
    reap_orphans,
    recheck_issue,
    reconcile_intake_pass,
    record_audit_event,
    record_manifest,
    record_pr_approval,
    record_suspension,
    record_usage_metrics,
    reset_task_coordination,
    resolve_conflicts,
    review_code,
    review_plan,
    review_security,
    run_checks,
    run_implementation_stage,
    scan_change,
    synchronize_branch,
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
    read_ticket_status,
    record_pr_approval,
    promote_pr,
    reap_orphans,
    reset_task_coordination,
    record_suspension,
    clear_suspension,
    run_checks,
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


def _warm_acli_auth(settings) -> None:
    """Authenticate the cloud Jira acli client once at worker startup.

    The acli credential lives in an ephemeral container path that an image
    rebuild or task replacement discards, so ticket activities re-authenticate
    from the configured credential here rather than failing on first use.
    """
    from services.integration.jira.acli_auth import ensure_acli_authenticated
    from services.integration.jira.credentials import (
        CLOUD,
        build_jira_credential_resolver,
    )
    from services.integration.tickets import JIRA, resolve_ticket_backend_name

    if resolve_ticket_backend_name(settings) != JIRA:
        return
    try:
        resolver = build_jira_credential_resolver(settings)
        for project in sorted(resolver.projects()):
            cred = resolver.resolve(project)
            if cred is not None and cred.auth_mode == CLOUD:
                ensure_acli_authenticated(cred, acli_path=settings.jira_acli_path)
                log.info("acli authenticated for cloud Jira at startup")
                return
    except Exception as exc:  # noqa: BLE001
        log.warning("acli startup authentication skipped: %s", exc)


async def main() -> None:
    settings = get_settings()
    role = _resolve_role()
    service_name = f"workflow-worker.{role}"
    configure_tracing(service_name)
    configure_metrics(service_name)
    configure_logging(service_name)
    install_litellm_header_propagation(settings.rebar_llm_base_url)
    _warm_acli_auth(settings)
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
