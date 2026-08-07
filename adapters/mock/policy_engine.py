"""In-memory policy engine backend for hermetic tests."""

from __future__ import annotations

from adapters.interfaces.policy_engine import PROCEED, PolicyDecision, PolicyInput


class MockPolicyEngine:
    def __init__(self, decision: PolicyDecision | None = None) -> None:
        self._decision = decision
        self.calls: list[PolicyInput] = []

    def seed(self, decision: PolicyDecision) -> None:
        self._decision = decision

    async def evaluate(self, decision_input: PolicyInput) -> PolicyDecision:
        self.calls.append(decision_input)
        if self._decision is not None:
            return self._decision
        return PolicyDecision(allow=True, bundle_version="mock", reasons=(), next_action=PROCEED)
