"""Temporal activity that drives a single Jira intake reconcile pass."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

from temporalio import activity

from services.orchestration.intake_reconciler import IntakeStart


@dataclass
class IntakeReconcileRequest:
    pass


@dataclass
class IntakeReconcileReport:
    started: int
    duplicates: int
    held: int


def _remote_head(remote: str, branch: str) -> str:
    proc = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


class _TemporalStarter:
    def __init__(self, client: Any, settings: Any, gh_resolver: Any) -> None:
        self._client = client
        self._settings = settings
        self._gh = gh_resolver

    async def start(self, start: IntakeStart) -> str:
        from services.orchestration.temporal_workflows.draft_pull_request import (
            DraftPullRequestInput,
            DraftPullRequestWorkflow,
        )

        base = self._settings.github_default_base_branch
        base_sha = _remote_head(self._gh.git_remote_url(start.repo), base)
        workflow_id = f"draft-pr-{start.issue_key}-{start.execution_id}"
        await self._client.start_workflow(
            DraftPullRequestWorkflow.run,
            DraftPullRequestInput(
                task_id=start.task_id,
                execution_id=start.execution_id,
                repo=start.repo,
                base=base,
                base_sha=base_sha,
                task=start.summary,
                issue_key=start.issue_key,
                focus=start.summary,
                stored_issue=start.stored_issue,
                build_id=self._settings.agent_build_id,
                parked_status=self._settings.jira_parked_status,
                jira_project=start.jira_project,
            ),
            id=workflow_id,
            task_queue=self._settings.temporal_task_queue,
        )
        return workflow_id


class _NotificationsAlerter:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def unroutable(self, *, issue_key: str, project: str, reason: str) -> None:
        from services.orchestration.notifications import send_intake_unroutable_alert

        send_intake_unroutable_alert(
            issue_key=issue_key, project=project, reason=reason, settings=self._settings
        )


@activity.defn
async def reconcile_intake_pass(request: IntakeReconcileRequest) -> IntakeReconcileReport:
    from scripts.config import get_settings
    from temporalio.client import Client

    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.integration.github.credentials import build_github_credential_resolver
    from services.integration.jira.credentials import build_jira_credential_resolver
    from services.integration.jira.intake_read import JiraPollReadSource
    from services.orchestration.intake_reconciler import reconcile_intake

    settings = get_settings()
    jira_resolver = build_jira_credential_resolver(settings)
    gh_resolver = build_github_credential_resolver(settings)
    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    report = await reconcile_intake(
        read_source=JiraPollReadSource(settings=settings, resolver=jira_resolver),
        starter=_TemporalStarter(client, settings, gh_resolver),
        alerter=_NotificationsAlerter(settings),
        ledger=PostgresSideEffectLedger(),
        settings=settings,
        jira_resolver=jira_resolver,
    )
    return IntakeReconcileReport(
        started=sum(1 for record in report.started if not record.duplicate),
        duplicates=sum(1 for record in report.started if record.duplicate),
        held=len(report.held),
    )
