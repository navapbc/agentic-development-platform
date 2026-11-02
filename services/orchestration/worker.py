"""Temporal worker entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from observability import (
    configure_logging,
    configure_metrics,
    configure_tracing,
    install_litellm_header_propagation,
    instrument_llm_tracing,
    temporal_interceptors,
)
from scripts.config import configured_log_level, get_settings
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from services.orchestration.temporal_activities import (
    alert_human_intervention,
    analyze_ci_failure,
    apply_ci_repair,
    apply_task_status,
    capture_base_head,
    check_dependency_cycle,
    claim_work_unit,
    classify_test_failures,
    clear_suspension,
    commit_and_push,
    count_tick,
    create_draft_pr,
    detect_dependency,
    evaluate_gate,
    evaluate_promotion,
    finalize_work_claim,
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
    record_dependency_link,
    record_manifest,
    record_plan_evidence,
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
    sign_authorship,
    signal_dependency_ready,
    synchronize_branch,
    unmark_task_parked,
    verify_acceptance,
    verify_authorship_merge,
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
from services.orchestration.worker_versioning import deployment_config, set_current_version

logging.basicConfig(level=configured_log_level())
log = logging.getLogger("workflow-worker")

WORKER_ROLE_ALL = "all"
WORKER_ROLE_WORKFLOW = "workflow"
WORKER_ROLE_ACTIVITY = "activity"
WORKER_ROLE_REVIEW = "review"
_WORKER_ROLES = frozenset(
    {WORKER_ROLE_ALL, WORKER_ROLE_WORKFLOW, WORKER_ROLE_ACTIVITY, WORKER_ROLE_REVIEW}
)

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

_REVIEW_ACTIVITIES = [
    evaluate_gate,
    review_plan,
    review_code,
    review_security,
    verify_acceptance,
]

_ACTIVITIES = [
    invoke_model,
    persist_execution_state,
    record_audit_event,
    record_usage_metrics,
    gather_repo_context,
    record_plan_evidence,
    count_tick,
    evaluate_gate,
    claim_work_unit,
    finalize_work_claim,
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
    record_dependency_link,
    detect_dependency,
    signal_dependency_ready,
    capture_base_head,
    check_dependency_cycle,
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
    load_gate_plan,
    classify_test_failures,
    record_manifest,
    reconcile_intake_pass,
    sign_authorship,
    verify_authorship_merge,
]


def _all_activities() -> list:
    seen: set[int] = set()
    combined = []
    for activity in [*_ACTIVITIES, *_REVIEW_ACTIVITIES]:
        if id(activity) not in seen:
            seen.add(id(activity))
            combined.append(activity)
    return combined


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
    versioning: dict[str, Any] = {}
    if settings.worker_versioning_enabled:
        versioning = {"deployment_config": deployment_config(settings)}
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
    if role == WORKER_ROLE_REVIEW:
        return Worker(client, task_queue=task_queue, activities=_REVIEW_ACTIVITIES, **versioning)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=_WORKFLOWS,
        activities=_all_activities(),
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


async def _scrub_workspace_credentials(workspace_root: str) -> None:
    """Clear the credential releases before the per-invocation helper left in every checkout.

    A workspace is keyed by execution id and nothing deletes it, so the scrub that runs when a
    workspace is materialized again never reaches most of them.
    """
    from services.orchestration.workspace_scrub import scrub_workspace_credentials

    receipt = await asyncio.to_thread(scrub_workspace_credentials, Path(workspace_root))
    if receipt.rewritten:
        log.warning(
            "cleared a persisted git credential from %d of %d workspace checkouts under %s",
            len(receipt.rewritten),
            receipt.scanned,
            workspace_root,
        )
    if receipt.failed:
        log.error(
            "could not clear a persisted git credential from %d workspace checkouts: %s",
            len(receipt.failed),
            ", ".join(receipt.failed),
        )


async def main() -> None:
    settings = get_settings()
    role = _resolve_role()
    service_name = f"workflow-worker.{role}"
    configure_tracing(service_name)
    configure_metrics(service_name)
    configure_logging(service_name)
    instrument_llm_tracing()
    install_litellm_header_propagation(settings.rebar_llm_base_url)
    _warm_acli_auth(settings)
    from scripts.aws_clients import configure_aws_fips

    configure_aws_fips(settings)
    from database.schema_guard import wait_for_platform_schema

    await wait_for_platform_schema(settings.database_url)
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

    if role == WORKER_ROLE_REVIEW:
        task_queue = settings.review_task_queue
    else:
        task_queue = settings.temporal_task_queue

    if role in (WORKER_ROLE_REVIEW, WORKER_ROLE_ALL):
        from services.orchestration.review_signing import (
            audit_review_trust_root,
            review_signing_preflight,
        )

        review_signing_preflight(settings)
        change = await audit_review_trust_root(settings)
        if change is not None:
            log.info("recorded review trust-root change: %s", change)

    if role in (WORKER_ROLE_ACTIVITY, WORKER_ROLE_ALL):
        from services.integration.tickets.store_remote import tickets_store_preflight
        from services.orchestration.authorship import authorship_signing_preflight

        tickets_store_preflight(settings)
        authorship_signing_preflight(settings)
        await _scrub_workspace_credentials(settings.execution_workspace_root)

    if role in (WORKER_ROLE_ACTIVITY, WORKER_ROLE_REVIEW, WORKER_ROLE_ALL):
        from services.review.evidence import evidence_object_store_preflight

        evidence_object_store_preflight(settings)

    workers = [build_worker(client, task_queue, role)]
    queues = [task_queue]
    if role == WORKER_ROLE_ALL:
        workers.append(build_worker(client, settings.review_task_queue, WORKER_ROLE_REVIEW))
        queues.append(settings.review_task_queue)

    log.info(
        "worker started on task queues %s (role=%s, build=%s)",
        queues,
        role,
        settings.agent_build_id,
    )
    heartbeat_task = None
    if settings.health_heartbeat_file:
        from services.healthcheck import heartbeat

        heartbeat_task = asyncio.create_task(
            heartbeat(
                Path(settings.health_heartbeat_file),
                interval=settings.health_heartbeat_interval_seconds,
                alive=lambda: all(worker.is_running for worker in workers),
            )
        )
    runners = [worker.run() for worker in workers]
    if settings.worker_versioning_enabled:
        runners.append(set_current_version(client, settings, queues))
    try:
        await asyncio.gather(*runners)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
