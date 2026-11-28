"""Parse, authenticate, and route inbound Jira issue webhooks to the shared intake path.

JiraIntakeHandler runs one tenant's intake in the process that holds the tenant's Jira credentials.
TenantJiraIntakeHandler holds no Jira credentials. It verifies a delivery with its tenant's
secret and starts a JiraIssueIntakeWorkflow for the issue key on the tenant's task queue, so the
tenant's activity worker reads the issue with the tenant's credentials.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from services.integration.jira.credentials import JiraCredentialResolver
from services.orchestration.intake_reconciler import (
    IntakeContext,
    IntakeReport,
    process_issue,
)
from services.orchestration.task_queues import tenant_task_queue
from services.orchestration.temporal_activities.issue_intake import valid_issue_key
from services.orchestration.webhook.inbound import DispatchNotReady

if TYPE_CHECKING:
    from scripts.config import Settings

_SIGNATURE_HEADER = "X-Hub-Signature"
_SIGNATURE_PREFIX = "sha256="
_ACCEPTED_EVENTS = frozenset({"jira:issue_created", "jira:issue_updated"})


def verify_jira_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret:
        return False
    if not header.startswith(_SIGNATURE_PREFIX):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"{_SIGNATURE_PREFIX}{digest}", header)


def _project_of(issue: dict[str, Any], key: str) -> str:
    fields = issue.get("fields") or {}
    project = fields.get("project")
    if isinstance(project, dict) and project.get("key"):
        return str(project["key"])
    return key.split("-", 1)[0] if "-" in key else key


@dataclass(frozen=True)
class JiraIssueEvent:
    project: str
    key: str
    raw_issue: dict[str, Any]


def parse_jira_issue_event(payload: dict[str, Any]) -> JiraIssueEvent | None:
    if payload.get("webhookEvent") not in _ACCEPTED_EVENTS:
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None
    key = issue.get("key")
    if not key:
        return None
    return JiraIssueEvent(project=_project_of(issue, str(key)), key=str(key), raw_issue=issue)


def jira_delivery_id(payload: Mapping[str, Any]) -> str:
    issue = payload.get("issue") or {}
    issue_ref = str(issue.get("id") or issue.get("key") or "")
    parts = [issue_ref, str(payload.get("timestamp") or "")]
    changelog = payload.get("changelog")
    if isinstance(changelog, dict) and changelog.get("id"):
        parts.append(str(changelog["id"]))
    return ":".join(part for part in parts if part)


class JiraIntakeHandler:
    name = "jira_intake"

    def __init__(
        self,
        *,
        tenant: str,
        secret: str,
        jira_resolver: JiraCredentialResolver,
        context: IntakeContext,
    ) -> None:
        self._tenant = tenant
        self._secret = secret
        self._resolver = jira_resolver
        self._context = context

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_jira_signature(self._secret, body, headers.get(_SIGNATURE_HEADER, ""))

    def tenant(self) -> str:
        return self._tenant

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
        return jira_delivery_id(payload)

    def parse(self, payload: dict[str, Any]) -> JiraIssueEvent | None:
        return parse_jira_issue_event(payload)

    def allowlisted(self, event: JiraIssueEvent, allowed: frozenset[str]) -> bool:
        return event.project in allowed

    async def dispatch(self, event: JiraIssueEvent) -> dict[str, Any]:
        cred = self._resolver.resolve(event.project)
        if cred is None:
            raise DispatchNotReady(f"project {event.project} is not configured")
        report = IntakeReport()
        await process_issue(
            event.raw_issue, project=event.project, cred=cred, ctx=self._context, report=report
        )
        started = sum(1 for record in report.started if not record.duplicate)
        duplicates = sum(1 for record in report.started if record.duplicate)
        return {
            "started": started,
            "duplicates": duplicates,
            "held": len(report.held),
            "deferred": len(report.deferred),
        }


@dataclass(frozen=True)
class TenantIssueEvent:
    tenant: str
    project: str
    key: str
    delivery_id: str


class IssueIntakeStarter(Protocol):
    async def start(self, event: TenantIssueEvent) -> dict[str, Any]: ...


class TenantJiraIntakeHandler:
    """Admit a tenant's Jira delivery and hand only its issue key to the tenant."""

    name = "jira_intake"

    def __init__(self, *, tenant: str, secret: str, starter: IssueIntakeStarter) -> None:
        self._tenant = tenant
        self._secret = secret
        self._starter = starter

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_jira_signature(self._secret, body, headers.get(_SIGNATURE_HEADER, ""))

    def tenant(self) -> str:
        return self._tenant

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
        base = jira_delivery_id(payload)
        return f"{self._tenant}:{base}" if base else ""

    def parse(self, payload: dict[str, Any]) -> TenantIssueEvent | None:
        event = parse_jira_issue_event(payload)
        if event is None or not valid_issue_key(event.key):
            return None
        return TenantIssueEvent(
            tenant=self._tenant,
            project=event.key.split("-", 1)[0],
            key=event.key,
            delivery_id=self.delivery_id({}, payload),
        )

    def allowlisted(self, event: TenantIssueEvent, allowed: frozenset[str]) -> bool:
        return event.project in allowed

    async def dispatch(self, event: TenantIssueEvent) -> dict[str, Any]:
        return await self._starter.start(event)


def issue_intake_workflow_id(event: TenantIssueEvent) -> str:
    digest = hashlib.sha256(event.delivery_id.encode()).hexdigest()[:32]
    return f"jira-intake-{event.tenant or 'platform'}-{digest}"


class TemporalIssueIntake:
    """Start one JiraIssueIntakeWorkflow per delivery on the tenant's task queue."""

    def __init__(self, client: Any, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def task_queue(self, tenant: str) -> str:
        """Return the task queue of the tenant.

        No activity worker polls the base queue, so a delivery that names no tenant is refused.
        """
        if not tenant:
            raise DispatchNotReady(
                "the Jira delivery names no tenant, so no activity worker polls a task queue for "
                "its intake"
            )
        return tenant_task_queue(self._settings, tenant)

    async def start(self, event: TenantIssueEvent) -> dict[str, Any]:
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from services.orchestration.temporal_workflows.issue_intake import (
            JiraIssueIntakeInput,
            JiraIssueIntakeWorkflow,
        )

        workflow_id = issue_intake_workflow_id(event)
        task_queue = self.task_queue(event.tenant)
        try:
            await self._client.start_workflow(
                JiraIssueIntakeWorkflow.run,
                JiraIssueIntakeInput(issue_key=event.key),
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            return {"status": "duplicate", "workflow_id": workflow_id}
        return {"status": "started", "workflow_id": workflow_id, "task_queue": task_queue}
