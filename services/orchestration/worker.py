"""Temporal worker entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
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

from services import temporal_client
from services.orchestration.activity_heartbeat import ActivityHeartbeatInterceptor
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
    delete_run_model_key,
    detect_dependency,
    evaluate_gate,
    evaluate_promotion,
    finalize_work_claim,
    find_open_pr,
    gather_repo_context,
    intake_jira_issue,
    invoke_model,
    link_pr_to_issue,
    load_gate_plan,
    mark_task_parked,
    period_alert_thread,
    persist_execution_state,
    promote_pr,
    read_run_limits,
    read_tenant_budget,
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
    refresh_park_buttons,
    reset_task_coordination,
    resolve_conflicts,
    review_code,
    review_plan,
    review_security,
    run_checks,
    run_implementation_stage,
    scan_change,
    settle_park_posts,
    sign_authorship,
    signal_dependency_ready,
    synchronize_branch,
    unmark_task_parked,
    validate_tenant,
    verify_acceptance,
    verify_authorship_merge,
)
from services.orchestration.temporal_activities.alert_action import apply_alert_action
from services.orchestration.temporal_workflows import (
    BranchSyncWorkflow,
    CountingWorkflow,
    DraftPullRequestWorkflow,
    DurableTaskWorkflow,
    ImplementationWorkflow,
    IntakeWorkflow,
    JiraIssueIntakeWorkflow,
    PlanReviewWorkflow,
    PullRequestWorkflow,
    ReapWorkflow,
    TenantValidationWorkflow,
)
from services.orchestration.temporal_workflows.alert_action import AlertActionWorkflow
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
    JiraIssueIntakeWorkflow,
    TenantValidationWorkflow,
    AlertActionWorkflow,
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
    refresh_park_buttons,
    settle_park_posts,
    scan_change,
    evaluate_promotion,
    load_gate_plan,
    classify_test_failures,
    record_manifest,
    reconcile_intake_pass,
    intake_jira_issue,
    sign_authorship,
    verify_authorship_merge,
    delete_run_model_key,
    read_run_limits,
    read_tenant_budget,
    period_alert_thread,
    validate_tenant,
    apply_alert_action,
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
    heartbeat = ActivityHeartbeatInterceptor()
    if role == WORKER_ROLE_WORKFLOW:
        return Worker(
            client,
            task_queue=task_queue,
            workflows=_WORKFLOWS,
            workflow_runner=runner,
            interceptors=[heartbeat],
            **versioning,
        )
    attribution = _tenant_attribution(role, settings)
    if role == WORKER_ROLE_ACTIVITY:
        return Worker(
            client,
            task_queue=task_queue,
            activities=_ACTIVITIES,
            interceptors=[heartbeat, attribution, *_tenant_interceptors()],
            **versioning,
        )
    if role == WORKER_ROLE_REVIEW:
        return Worker(
            client,
            task_queue=task_queue,
            activities=_REVIEW_ACTIVITIES,
            interceptors=[heartbeat, attribution, *_tenant_interceptors()],
            **versioning,
        )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=_WORKFLOWS,
        activities=_all_activities(),
        workflow_runner=runner,
        interceptors=[heartbeat, attribution, *_tenant_interceptors()],
        **versioning,
    )


def _tenant_attribution(role: str, settings: Any) -> Any:
    from services.orchestration.tenant_attribution import (
        TenantAttributionInterceptor,
        recorded_execution_tenant,
    )

    if role == WORKER_ROLE_REVIEW:
        return TenantAttributionInterceptor(settings.tenant, lookup=recorded_execution_tenant)
    return TenantAttributionInterceptor(settings.tenant)


def _task_queues(role: str, settings: Any) -> list[str]:
    """Return the queues the role polls, refusing a review worker without a tenant."""
    from services.integration.credentials import ConfigError
    from services.orchestration.task_queues import (
        review_task_queue,
        run_task_queue,
        workflow_task_queues,
    )

    if role == WORKER_ROLE_WORKFLOW:
        return workflow_task_queues(settings)
    if role != WORKER_ROLE_REVIEW:
        return [run_task_queue(settings)]
    if not settings.tenant:
        raise ConfigError(
            "TENANT is not set. Every review worker serves the review queue of one tenant, and "
            "there is no default."
        )
    return [review_task_queue(settings, settings.tenant)]


def _tenant_interceptors() -> list[Any]:
    from services.integration.tenant_config import active_tenant_config
    from services.orchestration.tenant_pinning import TenantConfigInterceptor

    config = active_tenant_config()
    return [] if config is None else [TenantConfigInterceptor(config)]


async def _scrub_workspace_credentials(workspace_root: str) -> None:
    """Clear the credential releases before the per-invocation helper left in every checkout.

    A workspace is keyed by execution id and nothing deletes it, so the scrub that runs when a
    workspace is materialized again never reaches most of them.
    """
    from services.orchestration.workspace_scrub import scrub_workspace_credentials

    receipt = await asyncio.to_thread(scrub_workspace_credentials, Path(workspace_root))
    if receipt.rewritten:
        log.warning(
            "removed userinfo from the remote URLs of %d of %d workspace checkouts under %s",
            len(receipt.rewritten),
            receipt.scanned,
            workspace_root,
        )
    if receipt.failed:
        log.error(
            "could not remove userinfo from the remote URLs of %d workspace checkouts: %s",
            len(receipt.failed),
            ", ".join(receipt.failed),
        )


def _worker_runners(
    client: Client, settings: Any, role: str, workers: list[Worker], queues: list[str]
) -> list[Any]:
    """Run the workers, and let only a worker that polls workflow queues move the current version.

    An activity or review worker can start on a build before any workflow worker polls it, and
    making that build current would route new executions to a workflow queue nobody polls.
    """
    runners: list[Any] = [worker.run() for worker in workers]
    if settings.worker_versioning_enabled and role in (WORKER_ROLE_WORKFLOW, WORKER_ROLE_ALL):
        runners.append(set_current_version(client, settings, queues))
    return runners


def _shut_down_on_sigterm(workers: list[Worker]) -> None:
    """Stop polling on SIGTERM and wait for the running activities before the process exits.

    Python's default SIGTERM action ends the process at once, which leaves a rebar store write
    lock that an activity holds on the shared volume until rebar's age ceiling expires.
    """
    loop = asyncio.get_running_loop()
    shutdowns: set[asyncio.Task[None]] = set()

    def shut_down() -> None:
        log.info("received SIGTERM, shutting down %d workers", len(workers))
        for worker in workers:
            task = loop.create_task(worker.shutdown())
            shutdowns.add(task)
            task.add_done_callback(shutdowns.discard)

    loop.add_signal_handler(signal.SIGTERM, shut_down)


async def main() -> None:
    settings = get_settings()
    role = _resolve_role()
    queues = _task_queues(role, settings)
    service_name = f"workflow-worker.{role}"
    configure_tracing(service_name)
    configure_metrics(service_name)
    configure_logging(service_name)
    instrument_llm_tracing()
    install_litellm_header_propagation(settings.rebar_llm_base_url)
    from scripts.aws_clients import configure_aws_fips

    configure_aws_fips(settings)
    from database.schema_guard import wait_for_platform_schema

    await wait_for_platform_schema(settings.database_url)
    log.info(
        "connecting to Temporal at %s (ns=%s)",
        settings.temporal_host,
        settings.temporal_namespace,
    )
    client = await temporal_client.connect(
        settings.temporal_host,
        settings,
        interceptors=temporal_interceptors(),
    )

    if role in (WORKER_ROLE_REVIEW, WORKER_ROLE_ALL):
        from services.orchestration.review_signing import (
            audit_review_trust_root,
            review_signing_preflight,
        )

        review_signing_preflight(settings)
        change = await audit_review_trust_root(settings)
        if change is not None:
            log.info("recorded review trust-root change: %s", change)

    tenant_refresh_task = None
    index_checkout_task = None
    if role in (WORKER_ROLE_ACTIVITY, WORKER_ROLE_REVIEW, WORKER_ROLE_ALL):
        from services.orchestration.tenant_pinning import (
            refresh_tenant_config,
            start_tenant_config,
        )

        tenant_config = start_tenant_config(settings)
        active_bundle = tenant_config.active
        log.info(
            "serving tenant %s on config bundle %s",
            tenant_config.tenant,
            active_bundle.id if active_bundle else "none",
        )
        tenant_refresh_task = asyncio.create_task(
            refresh_tenant_config(tenant_config, settings.tenant_config_refresh_seconds)
        )
    if role in (WORKER_ROLE_ACTIVITY, WORKER_ROLE_ALL):
        from services.integration.tickets.store_remote import tickets_store_preflight
        from services.orchestration.authorship import authorship_signing_preflight
        from services.orchestration.index_checkouts import keep_index_checkouts
        from services.orchestration.platform_escalation import platform_escalation_preflight
        from services.orchestration.tenant_pinning import install_tenant_schedules

        platform_escalation_preflight()
        index_checkout_task = asyncio.create_task(keep_index_checkouts(settings))
        await install_tenant_schedules(client, settings)
        tickets_store_preflight(settings)
        authorship_signing_preflight(settings)
        await _scrub_workspace_credentials(settings.execution_workspace_root)

    if role in (WORKER_ROLE_ACTIVITY, WORKER_ROLE_REVIEW, WORKER_ROLE_ALL):
        from services.review.evidence import evidence_object_store_preflight

        evidence_object_store_preflight(settings)

    workers = [build_worker(client, queue, role) for queue in queues]
    if role == WORKER_ROLE_ALL:
        from services.orchestration.task_queues import review_task_queue

        tenant_review_queue = review_task_queue(settings, settings.tenant)
        workers.append(build_worker(client, tenant_review_queue, WORKER_ROLE_REVIEW))
        queues.append(tenant_review_queue)

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
    watch_task = None
    if settings.aws_credentials_file:
        from services.aws_credentials import watch_credentials

        watch_task = asyncio.create_task(
            watch_credentials(
                Path(settings.aws_credentials_file),
                warn_seconds=settings.aws_credentials_warn_seconds,
            )
        )
    socket_mode = None
    approver_group_task = None
    if role in (WORKER_ROLE_ACTIVITY, WORKER_ROLE_ALL):
        from services.integration.slack.approver_groups import start_approver_group_refresh
        from services.integration.slack.socket_client import start_socket_mode

        socket_mode = await start_socket_mode(settings, client)
        approver_group_task = start_approver_group_refresh(settings)
    runners = _worker_runners(client, settings, role, workers, queues)
    _shut_down_on_sigterm(workers)
    try:
        await asyncio.gather(*runners)
    finally:
        for task in (
            heartbeat_task,
            watch_task,
            tenant_refresh_task,
            index_checkout_task,
            approver_group_task,
        ):
            if task is not None:
                task.cancel()
        if socket_mode is not None:
            socket_mode.close()


if __name__ == "__main__":
    asyncio.run(main())
