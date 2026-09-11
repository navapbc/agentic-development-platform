"""The R6 terminal-completion guard shared by all source-control backends.

A run completes from a merged or closed ticket status only after authenticated
status provenance, a matching publish result, passed gates, and verified branch
and artifact identifiers. When the ticket status and the source-control state
disagree, a recorded precedence rule decides. The precedence is source control.
"""

from __future__ import annotations

from dataclasses import dataclass

from adapters.interfaces.git_host import PublishResult

PRECEDENCE_SOURCE_CONTROL = "source_control"


@dataclass(frozen=True)
class CompletionInputs:
    ticket_status_terminal: bool
    source_control_terminal: bool
    status_provenance_authenticated: bool
    gates_passed: bool
    expected_branch: str
    expected_completion_signal_id: str
    publish_result: PublishResult | None


@dataclass(frozen=True)
class CompletionDecision:
    complete: bool
    reason: str
    precedence_applied: str = ""


def evaluate_completion(inputs: CompletionInputs) -> CompletionDecision:
    if not inputs.status_provenance_authenticated:
        return CompletionDecision(False, "unauthenticated_status")
    if inputs.publish_result is None:
        return CompletionDecision(False, "no_publish_result")
    if not inputs.gates_passed:
        return CompletionDecision(False, "gates_failed")
    if inputs.publish_result.branch != inputs.expected_branch:
        return CompletionDecision(False, "branch_mismatch")
    if inputs.publish_result.completion_signal_id != inputs.expected_completion_signal_id:
        return CompletionDecision(False, "artifact_mismatch")
    if inputs.ticket_status_terminal == inputs.source_control_terminal:
        return CompletionDecision(inputs.ticket_status_terminal, "agree")
    return CompletionDecision(
        inputs.source_control_terminal,
        "precedence_applied",
        precedence_applied=PRECEDENCE_SOURCE_CONTROL,
    )


def blocking_ticket_present_in_base(status: str, terminal_status: str) -> bool:
    """Return whether a blocking ticket has reached the terminal status.

    A dependent run may resume only once its blocker is terminal, which under the
    shared-base model means the blocker's change is merged into the base branch a
    dependent re-captures on resume. An empty terminal status never certifies.
    """
    if not terminal_status:
        return False
    return status.strip().lower() == terminal_status.strip().lower()
