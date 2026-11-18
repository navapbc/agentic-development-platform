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
    ci_exempt_repos: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_settings(
        cls, engine: PolicyEngine, settings: Settings | None = None
    ) -> PromotionPolicyMediator:
        from services.integration.repositories import LOCAL, load_repository_registry
        from services.integration.tickets.factory import ticket_projects

        resolved = settings or get_settings()
        registry = load_repository_registry(resolved)
        return cls(
            engine=engine,
            allowed_repos=frozenset(registry.names()),
            allowed_projects=frozenset(ticket_projects(resolved)),
            ci_exempt_repos=frozenset(registry.names(LOCAL)),
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
            ci_required=decision_input.repository not in self.ci_exempt_repos,
        )
        return await self.engine.evaluate(enriched)
