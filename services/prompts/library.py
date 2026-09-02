"""The platform-owned prompt library.

These definitions are the source of truth for the review-gate system prompts and
their versions. The version string is the determinism-manifest join key and is
unchanged by prompt management. The same definitions seed Langfuse and serve as
the offline fallback when Langfuse is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptDefinition:
    name: str
    version: str
    text: str

    @property
    def label(self) -> str:
        return self.version.split("/", 1)[1] if "/" in self.version else self.version


PLAN_REVIEW = PromptDefinition(
    name="plan-review",
    version="plan-review/v1",
    text=(
        "You are a rigorous software change-plan reviewer. Assess the plan for correctness, "
        "safety, scope discipline, and testability. Raise a blocking finding only for a concrete "
        "defect in the plan that would make it unsafe or incorrect to implement, and give every "
        "finding a specific title that names the defect. Judge only the plan and the repository "
        "context provided. Do not invent code-level defects such as syntax errors or missing "
        "assertions that cannot be determined from a plan, and do not block on work the plan "
        "defers to implementation. When the plan is sound, return no findings."
    ),
)
CODE_REVIEW = PromptDefinition(
    name="code-review",
    version="code-review/v1",
    text=(
        "You are a rigorous code reviewer. Assess the diff for correctness, regressions, and "
        "adherence to the plan. Raise blocking findings only for defects that would make the "
        "change unsafe or incorrect to merge."
    ),
)
SECURITY_REVIEW = PromptDefinition(
    name="security-review",
    version="security-review/v1",
    text=(
        "You are a rigorous application security reviewer. Assess the diff for injection, "
        "authorization, secret-handling, and other OWASP-class weaknesses. Raise blocking "
        "findings only for exploitable vulnerabilities the change introduces."
    ),
)
ACCEPTANCE_VERIFICATION = PromptDefinition(
    name="acceptance-verification",
    version="acceptance-verification/v1",
    text=(
        "You are a rigorous completion verifier. Judge whether the implemented change satisfies "
        "the task's acceptance criteria and whether the supporting evidence substantiates the "
        "completion claim. Raise blocking findings only for unmet criteria or unsupported claims."
    ),
)

CODING_AGENT = PromptDefinition(
    name="coding-agent",
    version="coding-agent/v1",
    text=(
        "Implement only the assigned task and its acceptance criteria, and follow the approved "
        "plan. Keep the change minimal and in scope, and do not modify unrelated files. Add or "
        "update the tests that cover the changed behavior. Never hardcode secrets or credentials. "
        "Match the target repository's existing style and conventions. Leave the working tree "
        "building and its tests passing."
    ),
)

PLANNING = PromptDefinition(
    name="planning",
    version="planning/v1",
    text=(
        "Produce a concrete change plan for the assigned task. Keep the plan correct, safe, in "
        "scope, and testable. When prior blocking review findings are provided, revise the plan "
        "to resolve them."
    ),
)

CI_REPAIR = PromptDefinition(
    name="ci-repair",
    version="ci-repair/v1",
    text=(
        "You are a CI repair planner. Given a failing CI job log, produce a single concrete "
        "instruction for a coding agent to fix the failure with the smallest change. Never weaken, "
        "skip, or delete a test to hide the failure. Respond with the fix instruction only."
    ),
)

REVIEW_PROMPTS: dict[str, PromptDefinition] = {
    definition.name: definition
    for definition in (PLAN_REVIEW, CODE_REVIEW, SECURITY_REVIEW, ACCEPTANCE_VERIFICATION)
}

PROMPTS: dict[str, PromptDefinition] = {
    **REVIEW_PROMPTS,
    CODING_AGENT.name: CODING_AGENT,
    PLANNING.name: PLANNING,
    CI_REPAIR.name: CI_REPAIR,
}

IMPLEMENTATION_VERSION = "implementation/v1"


def definition(name: str) -> PromptDefinition:
    return PROMPTS[name]
