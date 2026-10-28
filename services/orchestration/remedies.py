"""Operator remedy text for the human-intervention alerts raised by the draft-PR workflow.

Terminal reasons cross the Temporal payload boundary as plain ``str`` because an enum-typed
field raises ``TypeError`` in the data converter when a worker receives a token it does not
know, which fails the workflow task and retries forever. Tokens are narrowed to an enum here,
inside the process that renders the remedy, so an unrecognised token degrades to generic
guidance instead of stalling the run.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import assert_never


def _issue(issue_key: str) -> str:
    return issue_key or "<issue_key>"


def restart_command(issue_key: str) -> str:
    return f"`inv restart --issue {_issue(issue_key)}`"


def generic_remedy(issue_key: str, stage: str) -> str:
    return (
        f"Inspect the {stage} stage in the Temporal workflow history for this execution to find "
        f"the underlying failure, resolve it, then run {restart_command(issue_key)} to start a "
        "fresh execution."
    )


def _budget_remedy(issue_key: str, execution_id: str, limit: str) -> str:
    return (
        f"The run stopped after exhausting its {limit}. Run `inv cost-rollup --execution-id "
        f"{execution_id or '<execution_id>'}` to see which stage consumed the budget. Raise the "
        f"corresponding limit in the environment config or narrow the task scope, then run "
        f"{restart_command(issue_key)}."
    )


def _claim_remedy(issue_key: str) -> str:
    return (
        "Another execution already holds the work claim for this issue. Wait for that run to "
        f"reach a terminal state, then run {restart_command(issue_key)} if the issue still needs "
        "work."
    )


class ImplementationReason(StrEnum):
    WORK_UNIT_CLAIMED = "work_unit_claimed"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
    TOOL_CALLS_EXCEEDED = "tool_calls_exceeded"
    WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
    ERROR = "error"
    IMPL_FAILED = "impl_failed"


def implementation_remedy(token: str, *, issue_key: str, execution_id: str) -> str:
    try:
        reason = ImplementationReason(token)
    except ValueError:
        return generic_remedy(issue_key, "implementation")
    match reason:
        case ImplementationReason.WORK_UNIT_CLAIMED:
            return _claim_remedy(issue_key)
        case ImplementationReason.COST_BUDGET_EXCEEDED:
            return _budget_remedy(issue_key, execution_id, "cost budget")
        case ImplementationReason.TOKEN_BUDGET_EXCEEDED:
            return _budget_remedy(issue_key, execution_id, "token budget")
        case ImplementationReason.LLM_CALLS_EXCEEDED:
            return _budget_remedy(issue_key, execution_id, "LLM call budget")
        case ImplementationReason.TOOL_CALLS_EXCEEDED:
            return (
                "The coding agent hit its tool-call cap before finishing. Narrow the task scope "
                "or raise the tool-call cap in the environment config, then run "
                f"{restart_command(issue_key)}."
            )
        case ImplementationReason.WALL_CLOCK_EXCEEDED:
            return (
                "The coding agent hit its wall-clock cap before finishing. Narrow the task scope "
                "or raise the wall-clock cap in the environment config, then run "
                f"{restart_command(issue_key)}."
            )
        case ImplementationReason.ERROR | ImplementationReason.IMPL_FAILED:
            return (
                "The coding agent failed before producing a diff. Read the agent transcript "
                "linked from the implementation stage in the Temporal workflow history, fix the "
                f"blocking condition, then run {restart_command(issue_key)}."
            )
        case _:
            assert_never(reason)


class BranchSyncReason(StrEnum):
    HUMAN_COMMITS = "human_commits"
    REBASE_CAP_EXCEEDED = "rebase_cap_exceeded"
    MATERIAL_CONFLICT = "material_conflict"
    EXECUTION_INVALIDATING = "execution_invalidating"
    MATERIAL_CHANGE = "material_change"
    SYNC_PAUSED = "sync_paused"


def branch_sync_remedy(token: str, *, issue_key: str, branch: str) -> str:
    target = f"`{branch}`" if branch else "the run branch"
    try:
        reason = BranchSyncReason(token)
    except ValueError:
        return generic_remedy(issue_key, "branch_sync")
    match reason:
        case BranchSyncReason.HUMAN_COMMITS:
            return (
                f"A person pushed commits to {target}, so the run stopped rather than rewrite "
                "their work. Review those commits and either finish the change by hand or drop "
                f"the branch, then run {restart_command(issue_key)}."
            )
        case BranchSyncReason.REBASE_CAP_EXCEEDED:
            return (
                f"{target} fell too far behind the base branch to rebase within the configured "
                "attempt cap. Rebase it by hand or drop the branch, then run "
                f"{restart_command(issue_key)}."
            )
        case BranchSyncReason.MATERIAL_CONFLICT:
            return (
                f"Rebasing {target} produced a conflict in files the change itself edits, which "
                "the platform will not resolve on its own. Resolve the conflict by hand or drop "
                f"the branch, then run {restart_command(issue_key)}."
            )
        case BranchSyncReason.EXECUTION_INVALIDATING:
            return (
                "The ticket changed in a way that invalidates this run, for example a different "
                "project or issue type, or a status that withdraws it. Confirm the ticket is "
                "still "
                f"meant to be worked, then run {restart_command(issue_key)} to pick up the "
                "current ticket content."
            )
        case BranchSyncReason.MATERIAL_CHANGE:
            return (
                "The ticket requirements changed after the run started, so the work in progress "
                "no longer matches the ask. Review the updated ticket, then run "
                f"{restart_command(issue_key)} to rerun against the current requirements."
            )
        case BranchSyncReason.SYNC_PAUSED:
            return generic_remedy(issue_key, "branch_sync")
        case _:
            assert_never(reason)


class RepairReason(StrEnum):
    REPAIR_EXHAUSTED = "repair_exhausted"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    LLM_CALLS_EXCEEDED = "llm_calls_exceeded"


def repair_remedy(token: str, *, issue_key: str, execution_id: str) -> str:
    try:
        reason = RepairReason(token)
    except ValueError:
        return generic_remedy(issue_key, "repair")
    match reason:
        case RepairReason.REPAIR_EXHAUSTED:
            return (
                "Automated CI repair ran out of attempts with checks still failing. Fix the "
                f"failing checks on the branch and run `inv repair --issue {_issue(issue_key)} "
                f"--retry` to resume. To end this run and have intake admit the ticket again, "
                f"run `inv restart --issue {_issue(issue_key)}`."
            )
        case RepairReason.COST_BUDGET_EXCEEDED:
            return _budget_remedy(issue_key, execution_id, "cost budget")
        case RepairReason.TOKEN_BUDGET_EXCEEDED:
            return _budget_remedy(issue_key, execution_id, "token budget")
        case RepairReason.LLM_CALLS_EXCEEDED:
            return _budget_remedy(issue_key, execution_id, "LLM call budget")
        case _:
            assert_never(reason)


class IssueRecheckReason(StrEnum):
    EXECUTION_INVALIDATING = "execution_invalidating"
    MATERIAL_CHANGE = "material_change"


def issue_recheck_remedy(token: str, *, issue_key: str) -> str:
    try:
        reason = IssueRecheckReason(token)
    except ValueError:
        return generic_remedy(issue_key, "approval")
    match reason:
        case IssueRecheckReason.EXECUTION_INVALIDATING:
            return (
                "The ticket changed during the run in a way that invalidates it, so promotion was "
                "cancelled and the draft PR was left open. Confirm the ticket is still meant to be "
                f"worked, close the draft PR if it is not, then run {restart_command(issue_key)}."
            )
        case IssueRecheckReason.MATERIAL_CHANGE:
            return (
                "The ticket requirements changed during the run, so promotion was cancelled and "
                "the draft PR was left open. Review the updated ticket, close the stale draft PR, "
                f"then run {restart_command(issue_key)} to rerun against the current requirements."
            )
        case _:
            assert_never(reason)


def gate_decision_remedy(issue_key: str, gate: str) -> str:
    name = gate or "<gate>"
    return (
        f"The run is parked waiting for a decision on the {name} gate. Review the linked evidence "
        f"and run `inv gate --issue {_issue(issue_key)} --gate {name} --approve` to continue, or "
        f"`inv gate --issue {_issue(issue_key)} --gate {name} --reject` to cancel the run."
    )


def gate_rejected_remedy(issue_key: str, gate: str) -> str:
    return (
        f"The {gate or '<gate>'} gate was rejected, so the run stopped and no pull request will "
        "be promoted. Address the rejection reason in the ticket or the branch, then run "
        f"{restart_command(issue_key)} to start a fresh execution."
    )


def review_blocked_remedy(issue_key: str, gates: str) -> str:
    return (
        f"The {gates or 'review'} review gate blocked the change and is configured to fail closed "
        "rather than ask a human. Address the findings reported by that gate on the branch, then "
        f"run {restart_command(issue_key)}."
    )


def plan_rejected_remedy(issue_key: str) -> str:
    return (
        "The plan review rejected the proposed approach, so no implementation ran. Clarify the "
        "ticket description or acceptance criteria so the plan reviewer can approve it, then run "
        f"{restart_command(issue_key)}."
    )


def policy_denied_remedy(issue_key: str, reasons: str) -> str:
    detail = f" The policy bundle reported {reasons}." if reasons else ""
    return (
        f"The policy bundle denied this change, so the run stopped.{detail} Bring the change or "
        "the ticket into line with the policy, or amend the policy bundle if the denial is wrong, "
        f"then run {restart_command(issue_key)}."
    )


def approval_rejected_remedy(issue_key: str) -> str:
    return (
        "A reviewer rejected the pull request, so promotion was cancelled and the draft PR was "
        "left open. Address the review feedback and reopen the work by running "
        f"{restart_command(issue_key)}, or close the draft PR if the change is not wanted."
    )


def claim_denied_remedy(issue_key: str) -> str:
    return _claim_remedy(issue_key)


def base_conflict_remedy(issue_key: str) -> str:
    return (
        f"The ticket carries more than one base-branch label, so intake cannot tell which branch "
        f"to build from. Leave a single base label on {_issue(issue_key)}, then run "
        f"`inv restart --issue {_issue(issue_key)}` to re-admit it."
    )


def existing_pr_remedy(issue_key: str, pr_url: str) -> str:
    return (
        f"A pull request for this ticket already exists at {pr_url}, so the run stopped rather "
        f"than open a second one. Review and merge it, or close it and run "
        f"`inv restart --issue {_issue(issue_key)}` to build the ticket again."
    )


def unroutable_remedy(issue_key: str, project: str, unlisted: Sequence[str] = ()) -> str:
    if unlisted:
        names = ", ".join(unlisted)
        return (
            f"The ticket names {names}, which the allowlist for this environment does not carry, "
            f"so intake will not build against it. Add {names} to allowed_repos in the github "
            f"config for this environment, or point the ticket at a listed repository, then run "
            f"`inv restart --issue {_issue(issue_key)}` to re-admit it."
        )
    return (
        f"Intake could not resolve a target repository for {_issue(issue_key)}. Add a repos marker "
        f"to the ticket description, or set a default_repo for the {project} project in the intake "
        f"config, then run `inv restart --issue {_issue(issue_key)}` to re-admit it."
    )


def checks_not_configured_remedy(issue_key: str, repos: Sequence[str]) -> str:
    names = ", ".join(repos) or "the target repository"
    return (
        f"No checks config is registered for {names}, so the run would have no gates to pass. Add "
        f"an entry for {names} to the checks config, then run `inv restart --issue "
        f"{_issue(issue_key)}` to re-admit the ticket."
    )


def unresolvable_base_remedy(issue_key: str) -> str:
    return (
        f"The base branch named on {_issue(issue_key)} does not exist on the target repository. "
        f"Push the branch, or correct the base label on the ticket, then run `inv restart --issue "
        f"{_issue(issue_key)}` to re-admit it."
    )
