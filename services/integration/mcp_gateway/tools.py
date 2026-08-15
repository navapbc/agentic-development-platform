"""Read-only tool catalog served by the MCP Gateway."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from adapters.interfaces.ci_host import BuildResult
from adapters.interfaces.git_host import CodeHit, FileContent, PRFeedback
from scripts.config import Settings, get_settings

from services.integration.jira.credentials import acceptance_field
from services.integration.jira.edits import classify_edit
from services.integration.jira.normalize import normalize_issue
from services.integration.mcp_gateway.evidence_reader import EvidenceReader, PostgresEvidenceReader

READ_TOOL_NAMES = (
    "jira_get_task",
    "jira_compare_snapshot",
    "github_read_file",
    "github_search_code",
    "github_read_pr_feedback",
    "jenkins_get_build_result",
    "security_get_findings",
    "evidence_read_artifact",
    "repo_get_context_package",
)

READ_TOOL_DESCRIPTIONS = {
    "jira_get_task": "Fetch a Jira issue payload by key.",
    "jira_compare_snapshot": "Classify how a Jira issue changed against a stored snapshot.",
    "github_read_file": "Read a file at a ref from an allowlisted repository.",
    "github_search_code": "Search code in an allowlisted repository.",
    "github_read_pr_feedback": "Read review comments and state for a pull request.",
    "jenkins_get_build_result": "Read the result of a Jenkins build.",
    "security_get_findings": "Read stored security findings for an execution.",
    "evidence_read_artifact": "Read a persisted evidence artifact by id.",
    "repo_get_context_package": "Fetch a repository context package for a focus area.",
}


class IssueReader(Protocol):
    def get_issue(self, issue_key: str) -> dict[str, Any]: ...


class RepoReader(Protocol):
    async def read_file(self, repo: str, path: str, ref: str) -> FileContent: ...

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]: ...

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback: ...


class BuildReader(Protocol):
    async def get_build(self, repo: str, job: str, build_number: int) -> BuildResult: ...


class ContextFetch(Protocol):
    async def __call__(self, repo: str, commit: str, focus: str) -> dict[str, Any]: ...


@dataclass
class ReadOnlyToolset:
    jira: IssueReader
    github: RepoReader
    jenkins: BuildReader
    evidence: EvidenceReader
    context_fetch: ContextFetch
    acceptance_field: str = ""

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ReadOnlyToolset:
        from adapters.real.ci_host import RealJenkins
        from adapters.real.git_host import RealGitHost

        from services.coordination.idempotency import PostgresSideEffectLedger
        from services.coordination.sequencing import PostgresSequenceGate
        from services.integration.github.mediator import GitHubMediator
        from services.integration.jenkins.mediator import JenkinsMediator
        from services.integration.jira.adapter import ProjectRoutingJiraReader
        from services.orchestration.repo_intelligence_client import fetch_context_package

        resolved = settings or get_settings()
        ledger = PostgresSideEffectLedger()
        gate = PostgresSequenceGate()
        return cls(
            jira=ProjectRoutingJiraReader(resolved),
            github=GitHubMediator.from_settings(
                RealGitHost.from_settings(), ledger, gate, resolved
            ),
            jenkins=JenkinsMediator.from_settings(
                RealJenkins.from_settings(), ledger, gate, resolved
            ),
            evidence=PostgresEvidenceReader(),
            context_fetch=fetch_context_package,
            acceptance_field=acceptance_field(resolved),
        )

    async def jira_get_task(self, issue_key: str) -> dict[str, Any]:
        return self.jira.get_issue(issue_key)

    async def jira_compare_snapshot(
        self, issue_key: str, stored_payload: dict[str, Any]
    ) -> dict[str, Any]:
        fresh = normalize_issue(
            self.jira.get_issue(issue_key), acceptance_field=self.acceptance_field
        ).to_dict()
        result = classify_edit(stored_payload, fresh)
        return {
            "category": result.category,
            "changed_fields": list(result.changed_fields),
            "requires_pause": result.requires_pause,
            "reason": result.reason,
            "fresh": fresh,
        }

    async def github_read_file(self, repo: str, path: str, ref: str) -> dict[str, Any]:
        return asdict(await self.github.read_file(repo, path, ref))

    async def github_search_code(self, repo: str, query: str, ref: str = "") -> dict[str, Any]:
        hits = await self.github.search_code(repo, query, ref)
        return {"hits": [asdict(hit) for hit in hits]}

    async def github_read_pr_feedback(self, repo: str, number: int) -> dict[str, Any]:
        return asdict(await self.github.read_pr_feedback(repo, number))

    async def jenkins_get_build_result(
        self, repo: str, job: str, build_number: int
    ) -> dict[str, Any]:
        return asdict(await self.jenkins.get_build(repo, job, build_number))

    async def security_get_findings(self, execution_id: str) -> dict[str, Any]:
        findings = await self.evidence.get_security_findings(execution_id)
        return findings or {}

    async def evidence_read_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = await self.evidence.get_artifact(artifact_id)
        return artifact or {}

    async def repo_get_context_package(
        self, repo: str, focus: str, commit: str = ""
    ) -> dict[str, Any]:
        return await self.context_fetch(repo, commit, focus)
