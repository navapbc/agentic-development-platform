"""Generic suspend reasons and version-gated resume decisions for durable flows."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

RESUME_SIGNAL = "resume"

SUSPEND_STATE_ATTR = "SuspendState"
SUSPEND_REASON_ATTR = "SuspendReason"
PARKED_BUILD_ID_ATTR = "ParkedByBuildId"
SUSPENDED_STATE = "suspended"


class SuspendReason(str, Enum):
    AWAITING_CI = "awaiting_ci"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_FIX = "awaiting_fix"
    AWAITING_BUDGET = "awaiting_budget"
    AWAITING_DEPENDENCY = "awaiting_dependency"
    AWAITING_PR_CLOSE = "awaiting_pr_close"
    AWAITING_HUMAN = "awaiting_human"


class ResumeOn(str, Enum):
    CI_RESULT = "ci_result"
    HUMAN_APPROVAL = "human_approval"
    NEWER_BUILD = "newer_build"
    BUDGET_RAISED = "budget_raised"
    DEPENDENCY_READY = "dependency_ready"
    PR_CLOSED = "pr_closed"


_DEFAULT_RESUME_ON = {
    SuspendReason.AWAITING_CI: ResumeOn.CI_RESULT,
    SuspendReason.AWAITING_REVIEW: ResumeOn.HUMAN_APPROVAL,
    SuspendReason.AWAITING_FIX: ResumeOn.NEWER_BUILD,
    SuspendReason.AWAITING_BUDGET: ResumeOn.BUDGET_RAISED,
    SuspendReason.AWAITING_DEPENDENCY: ResumeOn.DEPENDENCY_READY,
    SuspendReason.AWAITING_PR_CLOSE: ResumeOn.PR_CLOSED,
    SuspendReason.AWAITING_HUMAN: ResumeOn.HUMAN_APPROVAL,
}


def default_resume_on(reason: SuspendReason) -> ResumeOn:
    return _DEFAULT_RESUME_ON[reason]


@dataclass(frozen=True)
class Suspension:
    reason: SuspendReason
    resume_on: ResumeOn
    stage: str = ""
    parked_build_id: str = ""
    detail: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


def _numeric_parts(build_id: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", build_id))


def is_newer_build(current_build_id: str, parked_build_id: str) -> bool:
    current = _numeric_parts(current_build_id)
    parked = _numeric_parts(parked_build_id)
    if not current or not parked:
        return False
    return current > parked


def should_resume(
    suspension: Suspension, *, current_build_id: str = "", trigger: ResumeOn | None = None
) -> bool:
    if suspension.resume_on == ResumeOn.NEWER_BUILD:
        return is_newer_build(current_build_id, suspension.parked_build_id)
    return trigger == suspension.resume_on


def select_resumable(suspensions: list[Suspension], *, current_build_id: str) -> list[Suspension]:
    return [
        suspension
        for suspension in suspensions
        if suspension.resume_on == ResumeOn.NEWER_BUILD
        and is_newer_build(current_build_id, suspension.parked_build_id)
    ]
