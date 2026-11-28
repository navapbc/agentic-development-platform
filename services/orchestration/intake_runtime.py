"""Shared runtime dependencies for the Jira intake path, used by the poller and the webhook."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from scripts.config import Settings

from services import process
from services.integration.github.git_auth import NO_AUTH, GitAuth
from services.integration.jira.credentials import acceptance_field
from services.operator_remedy import Remedy
from services.orchestration.change_narrative import compose_commit_message
from services.orchestration.execution_reconciler import ORPHAN_RECLAIMED, ActiveExecution
from services.orchestration.intake_base import UnresolvableBaseBranch, declared_issue_key
from services.orchestration.intake_reconciler import IntakeContext, IntakeStart
from services.orchestration.model_path import (
    ModelPathEpisode,
    ModelPathGate,
    OutagePost,
    PostgresModelPathEpisodes,
    shared_probe,
)

_log = structlog.get_logger(__name__)


def remote_head(remote: str, branch: str, auth: GitAuth = NO_AUTH) -> str:
    proc = process.run(
        ["git", *auth.config_args(), "ls-remote", "--", remote, f"refs/heads/{branch}"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **auth.env()},
    )
    return proc.stdout.split()[0] if proc.stdout.strip() else ""


class TemporalStarter:
    def __init__(self, client: Any, settings: Any, gh_resolver: Any = None) -> None:
        self._client = client
        self._settings = settings
        self._gh = gh_resolver

    async def resolve_base(self, *, repo: str, branch: str) -> str:
        from services.integration.github.factory import build_git_auth_for, build_git_host

        return remote_head(
            build_git_host(self._settings).source_location(repo),
            branch,
            build_git_auth_for(self._settings)(repo),
        )

    async def start(self, start: IntakeStart) -> str:
        from services.integration.github.factory import build_git_auth_for, build_git_host
        from services.integration.repositories import LOCAL, load_repository_registry
        from services.orchestration.gates.engine_source import (
            load_gate_engine_configs,
            triage_engine,
        )
        from services.orchestration.temporal_workflows.draft_pull_request import (
            DraftPullRequestInput,
            DraftPullRequestWorkflow,
        )
        from services.orchestration.usage_rollup import BudgetCaps

        repository = load_repository_registry(self._settings).require(start.repo)
        base = start.base or repository.default_branch
        source_location = build_git_host(self._settings).source_location(start.repo)
        base_auth = build_git_auth_for(self._settings)(start.repo)
        base_sha = remote_head(source_location, base, base_auth)
        if start.base and not base_sha:
            raise UnresolvableBaseBranch(base, start.repo)
        run_test_triage = triage_engine(load_gate_engine_configs(self._settings)) is not None
        await_pr_close = repository.backend == LOCAL
        caps = BudgetCaps.from_settings(self._settings)
        workflow_id = f"draft-pr-{start.issue_key}-{start.execution_id}"
        from services.orchestration.task_queues import run_review_task_queue, run_task_queue

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
                review_task_queue=run_review_task_queue(self._settings),
                snapshot_version=start.snapshot_version,
                run_test_triage=run_test_triage,
                await_pr_close=await_pr_close,
                file_impact=list(start.file_impact),
                max_cost_usd=caps.max_cost_usd,
                max_llm_calls=caps.max_llm_calls,
                max_tokens=caps.max_tokens,
            ),
            id=workflow_id,
            task_queue=run_task_queue(self._settings),
        )
        return workflow_id


class NotificationsAlerter:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def unroutable(
        self, *, issue_key: str, project: str, reason: str, remedy: Remedy
    ) -> None:
        from services.orchestration.notifications import (
            render_remedy,
            send_intake_unroutable_alert,
        )

        send_intake_unroutable_alert(
            issue_key=issue_key,
            project=project,
            reason=reason,
            remedy=render_remedy(remedy, self._settings),
            settings=self._settings,
        )


class ModelPathNotifier:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def unavailable(
        self, *, hop: str, detail: str, issue_key: str, episode: str = ""
    ) -> list[OutagePost]:
        from services.orchestration.model_path_alerts import send_model_path_unavailable_alert
        from services.orchestration.notifications import render_admin_remedy
        from services.orchestration.remedies import model_path_unavailable_remedy

        posts = await asyncio.to_thread(
            send_model_path_unavailable_alert,
            hop=hop,
            detail=detail,
            issue_key=issue_key,
            remedy=render_admin_remedy(model_path_unavailable_remedy(hop), self._settings),
            settings=self._settings,
            episode=episode,
        )
        return [
            OutagePost(post.channel, post.ts, post.admin, post.owner_token, post.blocks)
            for post in posts or ()
        ]

    async def recovered(
        self,
        *,
        hop: str,
        issue_key: str,
        episode: ModelPathEpisode | None = None,
        resolver: str = "",
    ) -> None:
        from services.orchestration.model_path_alerts import send_model_path_recovered_alert
        from services.orchestration.notifications import render_remedy
        from services.orchestration.remedies import model_path_recovered_remedy

        tenant_post, admin_post = _episode_posts(episode)
        await asyncio.to_thread(
            send_model_path_recovered_alert,
            hop=hop,
            issue_key=issue_key,
            remedy=render_remedy(model_path_recovered_remedy(hop), self._settings),
            settings=self._settings,
            outage_post=tenant_post,
            admin_post=admin_post,
            resolver=resolver,
        )

    async def host_action(self, *, episode: ModelPathEpisode) -> None:
        from services.orchestration.notifications import post_thread_reply

        for post in _episode_posts(episode):
            if post is not None:
                await asyncio.to_thread(
                    post_thread_reply, post, text=episode.host_action, settings=self._settings
                )


def _episode_posts(episode: ModelPathEpisode | None) -> tuple[Any, Any]:
    from services.orchestration.notifications import SlackPostRef

    if episode is None:
        return None, None
    tenant = (
        SlackPostRef(episode.slack_channel, episode.slack_ts, blocks=episode.slack_blocks)
        if episode.slack_ts
        else None
    )
    admin = (
        SlackPostRef(
            episode.admin_slack_channel,
            episode.admin_slack_ts,
            admin=True,
            owner_token=episode.admin_slack_token,
            blocks=episode.admin_slack_blocks,
        )
        if episode.admin_slack_ts
        else None
    )
    return tenant, admin


def build_model_path_gate(
    settings: Settings, ledger: Any, *, client: Any = None
) -> ModelPathGate | None:
    """Return the intake gate on the model path, or None when ``MODEL_PATH_PROBE`` is off.

    With a Temporal ``client`` the gate resumes the tenant's model path parks on recovery.
    """
    if not settings.model_path_probe:
        return None
    from services.orchestration.model_path_parks import ModelPathParkRegistry

    return ModelPathGate(
        probe=shared_probe(settings),
        episodes=PostgresModelPathEpisodes(),
        alerter=ModelPathNotifier(settings),
        ledger=ledger,
        tenant=settings.tenant.strip(),
        parks=ModelPathParkRegistry(client) if client is not None else None,
    )


class OrphanReclaimAlerter:
    """Alerts the operators that the reaper reclaimed a run and audits the channels it reached.

    An execution row carries no separate issue key. Intake records the ticket key as the task id,
    so the task id addresses the ticket the alert comments on and the restart it names.
    """

    def __init__(self, settings: Any, audit: Callable[[Any], Awaitable[Any]] | None = None) -> None:
        self._settings = settings
        self._audit = audit

    async def reclaimed(self, execution: ActiveExecution, *, workflow_id: str, reason: str) -> None:
        from services.orchestration.notifications import (
            HumanInterventionAlert,
            render_remedy,
            send_human_intervention_alert,
        )
        from services.orchestration.remedies import orphan_reclaimed_remedy
        from services.orchestration.temporal_activities.persistence import (
            AuditEvent,
            record_audit_event,
        )

        stage = execution.state or "unknown"
        structured = orphan_reclaimed_remedy(execution.task_id, workflow_id)
        remedy = render_remedy(structured, self._settings)
        channels = await asyncio.to_thread(
            send_human_intervention_alert,
            HumanInterventionAlert(
                task_id=execution.task_id,
                execution_id=execution.execution_id,
                workflow_id=workflow_id,
                repo=execution.repository,
                issue_key=execution.task_id,
                reason=ORPHAN_RECLAIMED,
                stage=stage,
                detail=reason,
                remedy=remedy,
                structured_remedy=structured,
            ),
            self._settings,
        )
        audit = self._audit or record_audit_event
        await audit(
            AuditEvent(
                event_id=f"{execution.execution_id}:EXECUTION_ORPHAN_RECLAIMED",
                task_id=execution.task_id,
                execution_id=execution.execution_id,
                event_type="EXECUTION_ORPHAN_RECLAIMED",
                stage=stage,
                prev_state=execution.state or None,
                new_state="CANCELLED",
                workflow_id=workflow_id,
                reason=reason,
                remedy=remedy or None,
                alert_channels=list(channels),
            )
        )


def _this_tenant(executions: Any) -> list[Any]:
    """Limit a query to this process's tenant, whose worker owns the runs it admits and reaps."""
    from scripts.config import get_settings

    tenant = get_settings().tenant
    return [executions.c.tenant == tenant] if tenant else []


class PostgresExecutionSeeder:
    def __init__(self) -> None:
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from database.engine import shared_platform_engine

            self._engine = shared_platform_engine()
        return self._engine

    async def seed(self, *, execution_id: str, task_id: str, repo: str) -> None:
        from database.models.schema import executions
        from scripts.config import get_settings
        from sqlalchemy.dialects.postgresql import insert

        stmt = (
            insert(executions)
            .values(
                execution_id=execution_id,
                task_id=task_id,
                state="TASK_RECEIVED",
                repository=repo,
                tenant=get_settings().tenant or None,
            )
            .on_conflict_do_nothing(index_elements=[executions.c.execution_id])
        )
        async with self._get_engine().begin() as conn:
            await conn.execute(stmt)

    async def abandon(self, *, execution_id: str, reason: str) -> None:
        from database.models.schema import executions
        from sqlalchemy import update

        stmt = (
            update(executions)
            .where(executions.c.execution_id == execution_id)
            .where(executions.c.workflow_id.is_(None))
            .values(state="FAILED")
        )
        async with self._get_engine().begin() as conn:
            await conn.execute(stmt)
        _log.info("intake_execution_abandoned", execution_id=execution_id, reason=reason)

    async def active_workflow(self, *, issue_key: str, repo: str) -> str | None:
        """Return the workflow of a running execution, ignoring rows no workflow was started for.

        An execution is seeded before its workflow starts. A row without a workflow is a start
        in progress, which the intake ledger reports as in flight, or one that failed, which the
        orphan reclaimer closes, so neither is a run to treat the ticket as a duplicate of.
        """
        from database.models.schema import executions
        from sqlalchemy import select

        stmt = (
            select(executions.c.workflow_id)
            .where(executions.c.task_id == issue_key)
            .where(executions.c.repository == repo)
            .where(executions.c.workflow_id.is_not(None))
            .where(executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")))
            .where(*_this_tenant(executions))
            .order_by(executions.c.updated_at.desc())
            .limit(1)
        )
        async with self._get_engine().connect() as conn:
            row = (await conn.execute(stmt)).first()
        return None if row is None else row.workflow_id

    async def count_active_runs(self) -> int:
        from database.models.schema import executions
        from sqlalchemy import func, select

        stmt = (
            select(func.count())
            .select_from(executions)
            .where(executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")))
            .where(*_this_tenant(executions))
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
            executions.c.repository,
        ).where(
            executions.c.state.not_in(("COMPLETED", "FAILED", "CANCELLED")),
            *_this_tenant(executions),
        )
        async with self._get_engine().connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            ActiveExecution(
                execution_id=row.execution_id,
                task_id=row.task_id,
                workflow_id=row.workflow_id or "",
                updated_at=row.updated_at,
                state=row.state or "",
                repository=row.repository or "",
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
    from services.orchestration.tenant_jobs import build_tenant_hold

    execution_store = PostgresExecutionSeeder()
    starter = TemporalStarter(client, settings)
    ledger = PostgresSideEffectLedger()
    return IntakeContext(
        starter=starter,
        alerter=NotificationsAlerter(settings),
        ledger=ledger,
        execution_seeder=execution_store,
        snapshot_store=None,
        github_allow=frozenset(source_control_repos(settings)),
        field_id=acceptance_field(),
        settings=settings,
        active_guard=execution_store,
        base_resolver=starter,
        checks_repos=required_checks_repos(settings),
        model_path=build_model_path_gate(settings, ledger, client=client),
        tenant_hold=build_tenant_hold(settings, ledger, client=client),
    )
