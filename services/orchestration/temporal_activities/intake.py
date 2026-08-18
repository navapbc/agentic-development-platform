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


@activity.defn
async def reconcile_intake_pass(request: IntakeReconcileRequest) -> IntakeReconcileReport:
    from scripts.config import get_settings
    from temporalio.client import Client

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.integration.github.credentials import build_github_credential_resolver
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
    gh_resolver = build_github_credential_resolver(settings)
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    execution_store = PostgresExecutionSeeder()
    report = await reconcile_intake(
        read_source=JiraPollReadSource(settings=settings, resolver=jira_resolver),
        starter=TemporalStarter(client, settings, gh_resolver),
        alerter=NotificationsAlerter(settings),
        ledger=PostgresSideEffectLedger(),
        execution_seeder=execution_store,
        settings=settings,
        jira_resolver=jira_resolver,
        active_guard=execution_store,
    )
    return IntakeReconcileReport(
        started=sum(1 for record in report.started if not record.duplicate),
        duplicates=sum(1 for record in report.started if record.duplicate),
        held=len(report.held),
    )

