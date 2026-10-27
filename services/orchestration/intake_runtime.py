"""Shared runtime dependencies for the Jira intake path, used by the poller and the webhook."""

from __future__ import annotations

from typing import Any

import structlog
from scripts.config import Settings

from services import process
from services.integration.jira.credentials import acceptance_field
from services.orchestration.change_narrative import compose_commit_message
from services.orchestration.execution_reconciler import ActiveExecution
from services.orchestration.intake_base import UnresolvableBaseBranch, declared_issue_key
from services.orchestration.intake_reconciler import IntakeContext, IntakeStart

_log = structlog.get_logger(__name__)


def remote_head(remote: str, branch: str) -> str:
    proc = process.run(
        ["git", "ls-remote", "--", remote, f"refs/heads/{branch}"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


class TemporalStarter:
    def __init__(self, client: Any, settings: Any, gh_resolver: Any = None) -> None:
        self._client = client
        self._settings = settings
        self._gh = gh_resolver

    async def resolve_base(self, *, repo: str, branch: str) -> str:
        from services.integration.github.factory import build_git_host

        return remote_head(build_git_host(self._settings).source_location(repo), branch)

    async def start(self, start: IntakeStart) -> str:
        from services.integration.github.factory import (
            LOCAL,
            build_git_host,
            resolve_source_control_backend_name,
        )
        from services.orchestration.gates.engine_source import (
            load_gate_engine_configs,
            triage_engine,
        )
        from services.orchestration.temporal_workflows.draft_pull_request import (
            DraftPullRequestInput,
            DraftPullRequestWorkflow,
        )
        from services.orchestration.usage_rollup import BudgetCaps

        base = start.base or self._settings.github_default_base_branch
        source_location = build_git_host(self._settings).source_location(start.repo)
        base_sha = remote_head(source_location, base)
        if start.base and not base_sha:
            raise UnresolvableBaseBranch(base, start.repo)
        run_test_triage = triage_engine(load_gate_engine_configs(self._settings)) is not None
        await_pr_close = resolve_source_control_backend_name(self._settings) == LOCAL
        caps = BudgetCaps.from_settings(self._settings)
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
                task_description=start.description,
                commit_message=compose_commit_message(
                    issue_key=declared_issue_key(start.stored_issue.get("labels") or ())
                    or start.issue_key,
                    summary=start.summary,
                    description=start.description,
                ),
                stored_issue=start.stored_issue,
                build_id=self._settings.agent_build_id,
                parked_status=self._settings.jira_parked_status,
                jira_project=start.jira_project,
                run_planning=True,
                run_reviews=self._settings.intake_run_reviews,
                run_policy=self._settings.intake_run_policy,
                run_approval=self._settings.intake_run_approval,
                run_dependency_detection=self._settings.dependency_detection_enabled,
                max_dependency_replans=self._settings.dependency_replan_attempts,
                review_task_queue=self._settings.review_task_queue,
                snapshot_version=start.snapshot_version,
                run_test_triage=run_test_triage,
                await_pr_close=await_pr_close,
                file_impact=list(start.file_impact),
                max_cost_usd=caps.max_cost_usd,
                max_llm_calls=caps.max_llm_calls,
                max_tokens=caps.max_tokens,
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

    async def count_active_runs(self) -> int:
        from database.models.schema import executions
        from sqlalchemy import func, select

        stmt = (
            select(func.count())
            .select_from(executions)
            .where(executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")))
        )
        async with self._get_engine().connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())

    async def list_active_executions(self) -> list[ActiveExecution]:
        from database.models.schema import executions
        from sqlalchemy import select

        stmt = select(
            executions.c.execution_id,
            executions.c.task_id,
            executions.c.workflow_id,
            executions.c.updated_at,
            executions.c.state,
        ).where(executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")))
        async with self._get_engine().connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            ActiveExecution(
                execution_id=row.execution_id,
                task_id=row.task_id,
                workflow_id=row.workflow_id or "",
                updated_at=row.updated_at,
                state=row.state or "",
            )
            for row in rows
        ]

    async def mark_orphaned(self, execution_id: str, *, reason: str) -> bool:
        from database.models.schema import executions
        from sqlalchemy import update

        stmt = (
            update(executions)
            .where(executions.c.execution_id == execution_id)
            .where(executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")))
            .values(state="CANCELLED")
        )
        async with self._get_engine().begin() as conn:
            result = await conn.execute(stmt)
        reclaimed = (result.rowcount or 0) > 0
        if reclaimed:
            _log.info("execution_orphan_reclaimed", execution_id=execution_id, reason=reason)
        return reclaimed


class TemporalWorkflowLiveness:
    """Reports whether a workflow is still running, treating a missing workflow as not live."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def is_live(self, workflow_id: str) -> bool:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        try:
            description = await self._client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            raise
        return description.status in (
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.CONTINUED_AS_NEW,
        )


def build_intake_context(settings: Settings, *, client: Any, gh_resolver: Any) -> IntakeContext:
    from services.coordination.idempotency import PostgresSideEffectLedger
    from services.integration.source_control import source_control_repos
    from services.orchestration.checks.config import required_checks_repos

    execution_store = PostgresExecutionSeeder()
    starter = TemporalStarter(client, settings)
    return IntakeContext(
        starter=starter,
        alerter=NotificationsAlerter(settings),
        ledger=PostgresSideEffectLedger(),
        execution_seeder=execution_store,
        snapshot_store=None,
        github_allow=frozenset(source_control_repos(settings)),
        field_id=acceptance_field(settings),
        settings=settings,
        active_guard=execution_store,
        base_resolver=starter,
        checks_repos=required_checks_repos(settings),
    )
