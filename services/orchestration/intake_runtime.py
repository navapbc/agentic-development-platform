"""Shared runtime dependencies for the Jira intake path, used by the poller and the webhook."""

from __future__ import annotations

import subprocess
from typing import Any

from scripts.config import Settings

from services.integration.github.credentials import allowed_repos
from services.integration.jira.credentials import acceptance_field
from services.orchestration.intake_reconciler import IntakeContext, IntakeStart


def remote_head(remote: str, branch: str) -> str:
    proc = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


class TemporalStarter:
    def __init__(self, client: Any, settings: Any, gh_resolver: Any) -> None:
        self._client = client
        self._settings = settings
        self._gh = gh_resolver

    async def start(self, start: IntakeStart) -> str:
        from services.orchestration.gates.engine_source import (
            load_gate_engine_configs,
            triage_engine,
        )
        from services.orchestration.temporal_workflows.draft_pull_request import (
            DraftPullRequestInput,
            DraftPullRequestWorkflow,
        )

        base = self._settings.github_default_base_branch
        base_sha = remote_head(self._gh.git_remote_url(start.repo), base)
        run_test_triage = triage_engine(load_gate_engine_configs(self._settings)) is not None
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
                ci_job=f"{start.repo}-ci",
                run_planning=True,
                snapshot_version=start.snapshot_version,
                run_test_triage=run_test_triage,
            ),
            id=workflow_id,
            task_queue=self._settings.temporal_task_queue,
        )
        return workflow_id


class NotificationsAlerter:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def unroutable(self, *, issue_key: str, project: str, reason: str) -> None:
        from services.orchestration.notifications import send_intake_unroutable_alert

        send_intake_unroutable_alert(
            issue_key=issue_key, project=project, reason=reason, settings=self._settings
        )


class PostgresExecutionSeeder:
    def __init__(self) -> None:
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from scripts.config import get_settings
            from sqlalchemy.ext.asyncio import create_async_engine

            self._engine = create_async_engine(
                get_settings().database_url, future=True, pool_pre_ping=True
            )
        return self._engine

    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None:
        from database.models.schema import executions
        from sqlalchemy.dialects.postgresql import insert

        stmt = (
            insert(executions)
            .values(
                execution_id=execution_id,
                task_id=task_id,
                state="TASK_RECEIVED",
                repository=repo,
            )
            .on_conflict_do_nothing(index_elements=[executions.c.execution_id])
        )
        async with self._get_engine().begin() as conn:
            await conn.execute(stmt)

    async def active_workflow(self, *, issue_key: str, repo: str) -> str | None:
        from database.models.schema import executions
        from sqlalchemy import select

        stmt = (
            select(executions.c.workflow_id)
            .where(executions.c.task_id == issue_key)
            .where(executions.c.repository == repo)
            .where(executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")))
            .order_by(executions.c.updated_at.desc())
            .limit(1)
        )
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        return row.workflow_id or ""


def build_intake_context(settings: Settings, *, client: Any, gh_resolver: Any) -> IntakeContext:
    from services.coordination.idempotency import PostgresSideEffectLedger

    execution_store = PostgresExecutionSeeder()
    return IntakeContext(
        starter=TemporalStarter(client, settings, gh_resolver),
        alerter=NotificationsAlerter(settings),
        ledger=PostgresSideEffectLedger(),
        execution_seeder=execution_store,
        snapshot_store=None,
        github_allow=frozenset(allowed_repos(settings)),
        field_id=acceptance_field(settings),
        settings=settings,
        active_guard=execution_store,
    )
