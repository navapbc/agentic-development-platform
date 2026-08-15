"""Promotion policy mediator: allowlist-guarded facade over the policy engine."""

from __future__ import annotations

from dataclasses import dataclass

from adapters.interfaces.policy_engine import PolicyDecision, PolicyEngine, PolicyInput
from scripts.config import Settings, get_settings
from services.integration.github.credentials import allowed_repos


class RepoNotAllowedError(Exception):
    def __init__(self, repo: str) -> None:
        super().__init__(f"repository not allowlisted: {repo}")
        self.repo = repo


@dataclass
class PromotionPolicyMediator:
    engine: PolicyEngine
    allowed_repos: frozenset[str]

    @classmethod
    def from_settings(
        cls, engine: PolicyEngine, settings: Settings | None = None
    ) -> PromotionPolicyMediator:
        resolved = settings or get_settings()
        return cls(engine=engine, allowed_repos=frozenset(allowed_repos(resolved)))

    def _guard(self, repo: str) -> None:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)

    async def evaluate(self, decision_input: PolicyInput) -> PolicyDecision:
        self._guard(decision_input.repository)
        return await self.engine.evaluate(decision_input)
