"""Vendor-neutral contract for the promotion policy decisions the platform mediates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

PROCEED = "proceed"
BLOCK_AND_REPORT = "block_and_report"


@dataclass(frozen=True)
class PolicyGate:
    name: str
    required: bool
    verdict: str
    on_block: str = "fail_closed"


@dataclass(frozen=True)
class PolicyInput:
    jira_project: str
    repository: str
    ci_status: str
    repair_attempts: int
    max_repair_attempts: int
    jira_snapshot_current: bool
    pr_authorized: bool
    gates: list[PolicyGate] = field(default_factory=list)
    required_gates: list[str] = field(default_factory=list)
    severity_counts: dict[str, int] = field(default_factory=dict)
    scan_findings: list[dict[str, str]] = field(default_factory=list)
    allowed_projects: list[str] = field(default_factory=list)
    allowed_repos: list[str] = field(default_factory=list)
    ci_required: bool = True

    def as_document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    bundle_version: str
    reasons: tuple[str, ...]
    next_action: str
    human_review: tuple[str, ...] = ()


class PolicyEngine(Protocol):
    async def evaluate(self, decision_input: PolicyInput) -> PolicyDecision: ...
