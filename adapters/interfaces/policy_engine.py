"""Vendor-neutral contract for the promotion policy decisions the platform mediates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

PROCEED = "proceed"
BLOCK_AND_REPORT = "block_and_report"


@dataclass(frozen=True)
class PolicyInput:
    jira_project: str
    repository: str
    ci_status: str
    blocking_findings: int
    scan_completed: bool
    plan_approved: bool
    repair_attempts: int
    max_repair_attempts: int
    jira_snapshot_current: bool
    acceptance_verified: bool
    pr_authorized: bool

    def as_document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    bundle_version: str
    reasons: tuple[str, ...]
    next_action: str


class PolicyEngine(Protocol):
    async def evaluate(self, decision_input: PolicyInput) -> PolicyDecision: ...
