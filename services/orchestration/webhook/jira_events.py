"""Parse, authenticate, and route inbound Jira issue webhooks to the shared intake path."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.integration.jira.credentials import JiraCredentialResolver
from services.orchestration.intake_reconciler import (
    IntakeContext,
    IntakeReport,
    process_issue,
)
from services.orchestration.webhook.inbound import DispatchNotReady

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


class JiraIntakeHandler:
    name = "jira_intake"

    def __init__(
        self, *, secret: str, jira_resolver: JiraCredentialResolver, context: IntakeContext
    ) -> None:
        self._secret = secret
        self._resolver = jira_resolver
        self._context = context

    def verify(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_jira_signature(self._secret, body, headers.get(_SIGNATURE_HEADER, ""))

    def delivery_id(self, headers: Mapping[str, str], payload: dict[str, Any]) -> str:
        issue = payload.get("issue") or {}
        issue_ref = str(issue.get("id") or issue.get("key") or "")
        parts = [issue_ref, str(payload.get("timestamp") or "")]
        changelog = payload.get("changelog")
        if isinstance(changelog, dict) and changelog.get("id"):
            parts.append(str(changelog["id"]))
        return ":".join(part for part in parts if part)

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
        return {"started": started, "duplicates": duplicates, "held": len(report.held)}
