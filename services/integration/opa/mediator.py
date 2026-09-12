"""Promotion policy mediator: allowlist-guarded facade over the policy engine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from adapters.interfaces.policy_engine import PolicyDecision, PolicyEngine, PolicyInput
from scripts.config import Settings, get_settings


class RepoNotAllowedError(Exception):
    def __init__(self, repo: str) -> None:
        super().__init__(f"repository not allowlisted: {repo}")
        self.repo = repo


@dataclass
class PromotionPolicyMediator:
    engine: PolicyEngine
    allowed_repos: frozenset[str]
    allowed_projects: frozenset[str] = field(default_factory=frozenset)
    ci_required: bool = True

    @classmethod
    def from_settings(
        cls, engine: PolicyEngine, settings: Settings | None = None
    ) -> PromotionPolicyMediator:
        from services.integration.github.factory import (
            GITHUB,
            resolve_source_control_backend_name,
        )
        from services.integration.source_control import source_control_repos
        from services.integration.tickets.factory import ticket_projects

        resolved = settings or get_settings()
        return cls(
            engine=engine,
            allowed_repos=frozenset(source_control_repos(resolved)),
            allowed_projects=frozenset(ticket_projects(resolved)),
            ci_required=resolve_source_control_backend_name(resolved) == GITHUB,
        )

    def _guard(self, repo: str) -> None:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)

    async def evaluate(self, decision_input: PolicyInput) -> PolicyDecision:
        self._guard(decision_input.repository)
        enriched = replace(
            decision_input,
            allowed_projects=sorted(self.allowed_projects),
            allowed_repos=sorted(self.allowed_repos),
            ci_required=self.ci_required,
        )
        return await self.engine.evaluate(enriched)
