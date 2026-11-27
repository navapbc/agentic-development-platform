"""Operator remedies for the human-intervention alerts raised by intake and the draft-PR workflow.

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

from services.operator_remedy import (
    Remedy,
    abandon_repair,
    approve_gate,
    config_key,
    reject_gate,
    remedy,
    restart_run,
    restore_model_path,
    resume_parked_run,
    retry_repair,
    roll_up_cost,
    worker_setting,
)


def generic_remedy(issue_key: str, stage: str) -> Remedy:
    return remedy(
        f"Inspect the {stage} stage in the Temporal workflow history for this execution to find "
        "the underlying failure, resolve it, then run ",
        restart_run(issue_key),
        " to start a fresh execution.",
    )


def _budget_remedy(issue_key: str, execution_id: str, limit: str, setting: str) -> Remedy:
    return remedy(
        f"The run stopped after exhausting its {limit}. Run ",
        roll_up_cost(execution_id),
        " to see which stage consumed the budget. Narrow the task scope, or raise ",
        worker_setting(setting),
        ". Then run ",
        restart_run(issue_key),
        ".",
    )


def _tenant_budget_remedy(issue_key: str) -> Remedy:
    return remedy(
        "The tenant's model budget for the current period is spent. Wait for the period to reset, "
        "or raise ",
        worker_setting("TENANT_MODEL_BUDGET_USD"),
        ". Then run ",
        restart_run(issue_key),
        ".",
    )


def _claim_remedy(issue_key: str) -> Remedy:
    return remedy(
        "Another execution already holds the work claim for this issue. Wait for that run to "
        "reach a terminal state, then run ",
        restart_run(issue_key),
        " if the issue still needs work.",
    )


class ImplementationReason(StrEnum):
    WORK_UNIT_CLAIMED = "work_unit_claimed"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    MAX_TOKENS_EXCEEDED = "token_budget_exceeded"
    LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
    RUN_MODEL_BUDGET_EXCEEDED = "run_model_budget_exceeded"
    TENANT_MODEL_BUDGET_EXCEEDED = "tenant_model_budget_exceeded"
    TOOL_CALLS_EXCEEDED = "tool_calls_exceeded"
    WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
    ERROR = "error"
    IMPL_FAILED = "impl_failed"


def implementation_remedy(token: str, *, issue_key: str, execution_id: str) -> Remedy:
    try:
        reason = ImplementationReason(token)
    except ValueError:
        return generic_remedy(issue_key, "implementation")
    match reason:
        case ImplementationReason.WORK_UNIT_CLAIMED:
            return _claim_remedy(issue_key)
        case ImplementationReason.COST_BUDGET_EXCEEDED:
            return _budget_remedy(
                issue_key, execution_id, "cost budget", "IMPLEMENTATION_MAX_COST_USD"
            )
        case ImplementationReason.MAX_TOKENS_EXCEEDED:
            return _budget_remedy(
                issue_key, execution_id, "token budget", "IMPLEMENTATION_MAX_TOKENS"
            )
        case ImplementationReason.LLM_CALLS_EXCEEDED:
            return _budget_remedy(
                issue_key, execution_id, "LLM call budget", "IMPLEMENTATION_MAX_LLM_CALLS"
            )
        case ImplementationReason.RUN_MODEL_BUDGET_EXCEEDED:
            return _budget_remedy(
                issue_key, execution_id, "model spend budget", "RUN_MODEL_BUDGET_USD"
            )
        case ImplementationReason.TENANT_MODEL_BUDGET_EXCEEDED:
            return _tenant_budget_remedy(issue_key)
        case ImplementationReason.TOOL_CALLS_EXCEEDED:
            return remedy(
                "The coding agent hit its tool-call cap before finishing. Narrow the task scope, "
                "or raise ",
                worker_setting("OPENHANDS_MAX_TOOL_CALLS"),
                ". Then run ",
                restart_run(issue_key),
                ".",
            )
        case ImplementationReason.WALL_CLOCK_EXCEEDED:
            return remedy(
                "The coding agent hit its wall-clock cap before finishing. Narrow the task scope, "
                "or raise ",
                worker_setting("OPENHANDS_MAX_WALL_CLOCK_SECONDS"),
                ". Then run ",
                restart_run(issue_key),
                ".",
            )
        case ImplementationReason.ERROR | ImplementationReason.IMPL_FAILED:
            return remedy(
                "The coding agent failed before producing a diff. Read the agent transcript "
                "linked from the implementation stage in the Temporal workflow history, fix the "
                "blocking condition, then run ",
                restart_run(issue_key),
                ".",
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


def branch_sync_remedy(token: str, *, issue_key: str, branch: str) -> Remedy:
    target = f"`{branch}`" if branch else "the run branch"
    try:
        reason = BranchSyncReason(token)
    except ValueError:
        return generic_remedy(issue_key, "branch_sync")
    match reason:
        case BranchSyncReason.HUMAN_COMMITS:
            return remedy(
                f"A person pushed commits to {target}, so the run stopped rather than rewrite "
                "their work. Review those commits and either finish the change by hand or drop "
                "the branch, then run ",
                restart_run(issue_key),
                ".",
            )
        case BranchSyncReason.REBASE_CAP_EXCEEDED:
            return remedy(
                f"{target} fell too far behind the base branch to rebase within the configured "
                "attempt cap. Rebase it by hand or drop the branch, then run ",
                restart_run(issue_key),
                ".",
            )
        case BranchSyncReason.MATERIAL_CONFLICT:
            return remedy(
                f"Rebasing {target} produced a conflict in files the change itself edits, which "
                "the platform will not resolve on its own. Resolve the conflict by hand or drop "
                "the branch, then run ",
                restart_run(issue_key),
                ".",
            )
        case BranchSyncReason.EXECUTION_INVALIDATING:
            return remedy(
                "The ticket changed in a way that invalidates this run, for example a different "
                "project or issue type, or a status that withdraws it. Confirm the ticket is "
                "still meant to be worked, then run ",
                restart_run(issue_key),
                " to pick up the current ticket content.",
            )
        case BranchSyncReason.MATERIAL_CHANGE:
            return remedy(
                "The ticket requirements changed after the run started, so the work in progress "
                "no longer matches the ask. Review the updated ticket, then run ",
                restart_run(issue_key),
                " to rerun against the current requirements.",
            )
        case BranchSyncReason.SYNC_PAUSED:
            return generic_remedy(issue_key, "branch_sync")
        case _:
            assert_never(reason)


class RepairReason(StrEnum):
    REPAIR_EXHAUSTED = "repair_exhausted"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    MAX_TOKENS_EXCEEDED = "token_budget_exceeded"
    LLM_CALLS_EXCEEDED = "llm_calls_exceeded"
    RUN_MODEL_BUDGET_EXCEEDED = "run_model_budget_exceeded"
    TENANT_MODEL_BUDGET_EXCEEDED = "tenant_model_budget_exceeded"


def repair_remedy(token: str, *, issue_key: str, execution_id: str) -> Remedy:
    try:
        reason = RepairReason(token)
    except ValueError:
        return generic_remedy(issue_key, "repair")
    match reason:
        case RepairReason.REPAIR_EXHAUSTED:
            return remedy(
                "Automated CI repair ran out of attempts with checks still failing. Fix the "
                "failing checks on the branch and run ",
                retry_repair(issue_key),
                " to resume, or run ",
                abandon_repair(issue_key),
                " to end the run.",
            )
        case RepairReason.COST_BUDGET_EXCEEDED:
            limit, setting = "cost budget", "IMPLEMENTATION_MAX_COST_USD"
        case RepairReason.MAX_TOKENS_EXCEEDED:
            limit, setting = "token budget", "IMPLEMENTATION_MAX_TOKENS"
        case RepairReason.LLM_CALLS_EXCEEDED:
            limit, setting = "LLM call budget", "IMPLEMENTATION_MAX_LLM_CALLS"
        case RepairReason.RUN_MODEL_BUDGET_EXCEEDED:
            limit, setting = "model spend budget", "RUN_MODEL_BUDGET_USD"
        case RepairReason.TENANT_MODEL_BUDGET_EXCEEDED:
            return remedy(
                "The tenant's model budget for the current period is spent. Wait for the period "
                "to reset, or raise ",
                worker_setting("TENANT_MODEL_BUDGET_USD"),
                ". A retry would stop again at the same budget, so run ",
                abandon_repair(issue_key),
                " to end the run.",
            )
        case _:
            assert_never(reason)
    return remedy(
        f"The repair stopped after exhausting its {limit}. Run ",
        roll_up_cost(execution_id),
        " to see which stage consumed the budget. Narrow the task scope, or raise ",
        worker_setting(setting),
        ". A retry would stop again at the same budget, so run ",
        abandon_repair(issue_key),
        " to end the run.",
    )


class IssueRecheckReason(StrEnum):
    EXECUTION_INVALIDATING = "execution_invalidating"
    MATERIAL_CHANGE = "material_change"


def issue_recheck_remedy(token: str, *, issue_key: str) -> Remedy:
    try:
        reason = IssueRecheckReason(token)
    except ValueError:
        return generic_remedy(issue_key, "approval")
    match reason:
        case IssueRecheckReason.EXECUTION_INVALIDATING:
            return remedy(
                "The ticket changed during the run in a way that invalidates it, so promotion was "
                "cancelled and the draft PR was left open. Confirm the ticket is still meant to be "
                "worked, close the draft PR if it is not, then run ",
                restart_run(issue_key),
                ".",
            )
        case IssueRecheckReason.MATERIAL_CHANGE:
            return remedy(
                "The ticket requirements changed during the run, so promotion was cancelled and "
                "the draft PR was left open. Review the updated ticket, close the stale draft PR, "
                "then run ",
                restart_run(issue_key),
                " to rerun against the current requirements.",
            )
        case _:
            assert_never(reason)


def gate_decision_remedy(issue_key: str, gate: str) -> Remedy:
    return remedy(
        f"The run is parked waiting for a decision on the {gate or '<gate>'} gate. Review the "
        "linked evidence and run ",
        approve_gate(issue_key, gate),
        " to continue, or ",
        reject_gate(issue_key, gate),
        " to cancel the run.",
    )


def gate_rejected_remedy(issue_key: str, gate: str) -> Remedy:
    return remedy(
        f"The {gate or '<gate>'} gate was rejected, so the run stopped and no pull request will "
        "be promoted. Address the rejection reason in the ticket or the branch, then run ",
        restart_run(issue_key),
        " to start a fresh execution.",
    )


def review_blocked_remedy(issue_key: str, gates: str) -> Remedy:
    return remedy(
        f"The {gates or 'review'} review gate blocked the change and is configured to fail closed "
        "rather than ask a human. Address the findings reported by that gate on the branch, then "
        "run ",
        restart_run(issue_key),
        ".",
    )


def plan_rejected_remedy(issue_key: str) -> Remedy:
    return remedy(
        "The plan review rejected the proposed approach, so no implementation ran. Clarify the "
        "ticket description or acceptance criteria so the plan reviewer can approve it, then run ",
        restart_run(issue_key),
        ".",
    )


def policy_denied_remedy(issue_key: str, reasons: str) -> Remedy:
    detail = f" The policy bundle reported {reasons}." if reasons else ""
    return remedy(
        f"The policy bundle denied this change, so the run stopped.{detail} Bring the change or "
        "the ticket into line with the policy, or amend the policy bundle if the denial is wrong, "
        "then run ",
        restart_run(issue_key),
        ".",
    )


def approval_rejected_remedy(issue_key: str) -> Remedy:
    return remedy(
        "A reviewer rejected the pull request, so promotion was cancelled and the draft PR was "
        "left open. Address the review feedback and reopen the work by running ",
        restart_run(issue_key),
        ", or close the draft PR if the change is not wanted.",
    )


def claim_denied_remedy(issue_key: str) -> Remedy:
    return _claim_remedy(issue_key)


def base_conflict_remedy(issue_key: str) -> Remedy:
    return remedy(
        "The ticket carries more than one base-branch label, so intake cannot tell which branch "
        f"to build from. Leave a single base label on {issue_key or '<issue_key>'}, then run ",
        restart_run(issue_key),
        " to re-admit it.",
    )


def abandoned_remedy(issue_key: str) -> Remedy:
    return remedy(
        "The run was abandoned, so it stopped without a pull request. Address the reason it was "
        "abandoned in the ticket or the repository, then run ",
        restart_run(issue_key),
        " to start a fresh execution.",
    )


def run_failed_remedy(issue_key: str, stage: str) -> Remedy:
    return remedy(
        f"The run failed at the {stage or '<stage>'} stage on an error the workflow does not "
        "handle, such as a model call that exhausted its retries, so it stopped without a pull "
        "request. Inspect that stage in the Temporal workflow history for this execution, "
        "resolve the cause, then run ",
        restart_run(issue_key),
        " to start a fresh execution.",
    )


def orphan_reclaimed_remedy(issue_key: str, workflow_id: str) -> Remedy:
    return remedy(
        f"The workflow {workflow_id or '<workflow_id>'} stopped running without settling its "
        "execution, so the reaper marked the execution CANCELLED and released its concurrency "
        "slot. Inspect the workflow history in Temporal to find why it stopped, resolve the "
        "cause, then run ",
        restart_run(issue_key),
        " to start a fresh execution.",
    )


MODEL_PATH_FAILURES = {
    "credentials": "the AWS session LiteLLM signs Bedrock requests with is expired or invalid",
    "bedrock_path": "LiteLLM cannot connect to the Bedrock runtime endpoint",
    "litellm": "the worker cannot reach the LiteLLM gateway",
}


MODEL_PATH_HOP_LABELS = {
    "credentials": "AWS credentials",
    "bedrock_path": "Bedrock connection",
    "litellm": "LiteLLM gateway",
}


def model_path_unavailable_remedy(hop: str) -> Remedy:
    return remedy(
        "Run ",
        restore_model_path(hop),
        ". The next intake pass then starts the deferred tickets with no further action.",
    )


def model_path_recovered_remedy(hop: str) -> Remedy:
    return remedy("None. If the path fails again, run ", restore_model_path(hop), ".")


def model_unavailable_park_remedy(issue_key: str, hop: str) -> Remedy:
    return remedy(
        "Run ",
        restore_model_path(hop),
        ". The next intake pass that finds the model path healthy resumes the run at the stage "
        "that failed, with the work it had done kept. To resume it before that pass, run ",
        resume_parked_run(issue_key),
        ".",
    )


def code_defect_remedy(detail: str) -> Remedy:
    return remedy(
        "A defect in ADP itself stopped the push, so the run is parked rather than failed. Fix "
        "the defect in ADP and deploy a newer build. The parked run resumes on that build on its "
        f"own, and parks again if the defect persists. The failure was {detail}."
    )


def existing_pr_remedy(issue_key: str, pr_url: str) -> Remedy:
    return remedy(
        f"A pull request for this ticket already exists at {pr_url}, so the run stopped rather "
        "than open a second one. Review and merge it, or close it and run ",
        restart_run(issue_key),
        " to build the ticket again.",
    )


def unroutable_remedy(
    issue_key: str, project: str, unlisted: Sequence[str] = (), *, ticket_config: str = "jira"
) -> Remedy:
    if unlisted:
        names = ", ".join(unlisted)
        return remedy(
            f"The ticket names {names}, which the tenant's repositories do not include, so "
            f"intake will not build against it. Add {names} under ",
            config_key("repositories", "repositories"),
            ", or point the ticket at a listed repository, then run ",
            restart_run(issue_key),
            " to re-admit it.",
        )
    return remedy(
        f"Intake could not resolve a target repository for {issue_key or '<issue_key>'}. Add a "
        "repos marker to the ticket description, or set ",
        config_key(ticket_config, "default_repo"),
        f" for the {project} project, then run ",
        restart_run(issue_key),
        " to re-admit it.",
    )


def checks_not_configured_remedy(issue_key: str, repos: Sequence[str]) -> Remedy:
    names = ", ".join(repos) or "the target repository"
    return remedy(
        f"No checks entry is registered for {names}, so the run would have no gates to pass. Add "
        f"an entry for {names} under ",
        config_key("checks", "checks"),
        ", then run ",
        restart_run(issue_key),
        " to re-admit the ticket.",
    )


def unresolvable_base_remedy(issue_key: str) -> Remedy:
    return remedy(
        f"The base branch named on {issue_key or '<issue_key>'} does not exist on the target "
        "repository. Push the branch, or correct the base label on the ticket, then run ",
        restart_run(issue_key),
        " to re-admit it.",
    )
