"""Level-triggered Jira read source that enumerates each project's open issues per poll."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.config import Settings

from services.integration.jira.acli_auth import ensure_acli_authenticated
from services.integration.jira.adapter import build_jira_adapter
from services.integration.jira.credentials import CLOUD, JiraCredentialResolver


def _candidate_jql(project: str) -> str:
    return f'project = "{project}" AND statusCategory != Done ORDER BY created ASC'


@dataclass(frozen=True)
class JiraPollReadSource:
    settings: Settings
    resolver: JiraCredentialResolver

    async def list_candidate_issues(self, project: str) -> list[dict[str, Any]]:
        cred = self.resolver.resolve(project)
        if cred is not None and cred.auth_mode == CLOUD:
            ensure_acli_authenticated(cred, acli_path=self.settings.jira_acli_path)
        adapter = build_jira_adapter(self.settings, project=project, resolver=self.resolver)
        return adapter.search_issues(_candidate_jql(project))
