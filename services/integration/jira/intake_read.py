"""Level-triggered Jira read source that enumerates each project's open issues per poll."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.config import Settings

from services.integration.jira.adapter import build_jira_adapter
from services.integration.jira.credentials import JiraCredentialResolver


def _candidate_jql(project: str, intake_label: str = "") -> str:
    clauses = [f'project = "{project}"', "statusCategory != Done"]
    if intake_label:
        clauses.append(f'labels = "{intake_label}"')
    return f"{' AND '.join(clauses)} ORDER BY created ASC"


@dataclass(frozen=True)
class JiraPollReadSource:
    settings: Settings
    resolver: JiraCredentialResolver

    async def list_candidate_issues(self, project: str) -> list[dict[str, Any]]:
        cred = self.resolver.resolve(project)
        adapter = build_jira_adapter(self.settings, project=project, resolver=self.resolver)
        label = cred.intake_label if cred is not None else ""
        return adapter.search_issues(_candidate_jql(project, label))
