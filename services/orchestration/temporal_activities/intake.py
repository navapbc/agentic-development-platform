"""Temporal activity that drives a single Jira intake reconcile pass."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity


@dataclass
class IntakeReconcileRequest:
    pass


@dataclass
class IntakeReconcileReport:
    started: int
    duplicates: int
    held: int
    deferred: int = 0
    blocked: int = 0


@activity.defn
async def reconcile_intake_pass(request: IntakeReconcileRequest) -> IntakeReconcileReport:
    from scripts.config import get_settings

    from services.integration.tickets import REBAR, resolve_ticket_backend_name

    if resolve_ticket_backend_name(get_settings()) == REBAR:
        return await _reconcile_rebar_pass()
    return await _reconcile_jira_pass()


async def _reclaim_orphaned_slots(client, execution_store, settings) -> None:
    from datetime import UTC, datetime, timedelta

    from services.orchestration.execution_reconciler import reconcile_orphaned_executions
    from services.orchestration.intake_runtime import TemporalWorkflowLiveness

    await reconcile_orphaned_executions(
        store=execution_store,
        liveness=TemporalWorkflowLiveness(client),
        now=datetime.now(UTC),
        grace=timedelta(seconds=settings.execution_orphan_grace_seconds),
    )


async def _reconcile_jira_pass() -> IntakeReconcileReport:
    from scripts.config import get_settings
    from temporalio.client import Client

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.integration.jira.credentials import build_jira_credential_resolver
    from services.integration.jira.intake_read import JiraPollReadSource
    from services.orchestration.intake_reconciler import reconcile_intake
    from services.orchestration.intake_runtime import (
        NotificationsAlerter,
        PostgresExecutionSeeder,
        TemporalStarter,
    )

    settings = get_settings()
    jira_resolver = build_jira_credential_resolver(settings)
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    execution_store = PostgresExecutionSeeder()
    starter = TemporalStarter(client, settings)
    await _reclaim_orphaned_slots(client, execution_store, settings)
    report = await reconcile_intake(
        read_source=JiraPollReadSource(settings=settings, resolver=jira_resolver),
        starter=starter,
        alerter=NotificationsAlerter(settings),
        ledger=PostgresSideEffectLedger(),
        execution_seeder=execution_store,
        settings=settings,
        jira_resolver=jira_resolver,
        active_guard=execution_store,
        run_counter=execution_store,
        base_resolver=starter,
    )
    return IntakeReconcileReport(
        started=sum(1 for record in report.started if not record.duplicate),
        duplicates=sum(1 for record in report.started if record.duplicate),
        held=len(report.held),
        deferred=len(report.deferred),
    )


async def _reconcile_rebar_pass() -> IntakeReconcileReport:
    import rebar
    from scripts.config import get_settings
    from scripts.rebar_poll import (
        FileWatermarkStore,
        GitTicketStoreReader,
        InMemoryIdempotencyGuard,
        RebarIdentityAccess,
        StructlogAuditSink,
        watermark_path,
    )
    from temporalio.client import Client

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.integration.jira.credentials import acceptance_field
    from services.integration.source_control import source_control_repos
    from services.integration.tickets.rebar_config import (
        load_poll_config,
        load_rebar_backend_config,
    )
    from services.orchestration.intake_reconciler import IntakeContext, build_admission
    from services.orchestration.intake_runtime import (
        NotificationsAlerter,
        PostgresExecutionSeeder,
        TemporalStarter,
    )
    from services.orchestration.rebar_intake import reconcile_rebar_intake

    settings = get_settings()
    backend = load_rebar_backend_config(settings)
    poll_config = load_poll_config(settings)
    repo_root = backend.repo_root
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    execution_store = PostgresExecutionSeeder()
    starter = TemporalStarter(client, settings)
    await _reclaim_orphaned_slots(client, execution_store, settings)

    ctx = IntakeContext(
        starter=starter,
        alerter=NotificationsAlerter(settings),
        ledger=PostgresSideEffectLedger(),
        execution_seeder=execution_store,
        snapshot_store=None,
        github_allow=frozenset(source_control_repos(settings)),
        field_id=acceptance_field(settings),
        settings=settings,
        active_guard=execution_store,
        admission=await build_admission(settings, execution_store),
        base_resolver=starter,
    )

    ready_ids = {str(t.get("ticket_id") or "") for t in rebar.ready(repo_root=repo_root)}

    report = await reconcile_rebar_intake(
        reader=GitTicketStoreReader(repo_root),
        watermark=FileWatermarkStore(watermark_path(repo_root)),
        audit=StructlogAuditSink(),
        access=RebarIdentityAccess(
            repo_root,
            reader_roles=poll_config.reader_roles,
            identity_roles=poll_config,
        ),
        allowed_fields=poll_config.allowed_fields,
        ctx=ctx,
        show_ticket=lambda tid: dict(rebar.show_ticket(tid, repo_root=repo_root)),
        is_ready=lambda tid: tid in ready_ids,
        project=backend.project,
        default_repo=backend.default_repo,
        idempotency=InMemoryIdempotencyGuard(),
    )
    return IntakeReconcileReport(
        started=report.started,
        duplicates=report.duplicates,
        held=report.held,
        deferred=report.deferred,
        blocked=report.blocked,
    )
