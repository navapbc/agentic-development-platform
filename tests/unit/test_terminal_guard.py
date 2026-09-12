from __future__ import annotations

from adapters.interfaces.git_host import PublishResult
from services.orchestration.terminal_guard import (
    PRECEDENCE_SOURCE_CONTROL,
    CompletionInputs,
    evaluate_completion,
)


def _publish(branch: str = "agent/T-1", signal_id: str = "7") -> PublishResult:
    return PublishResult(repo="demo", branch=branch, completion_signal_id=signal_id)


def _inputs(**overrides) -> CompletionInputs:
    base = dict(
        ticket_status_terminal=True,
        source_control_terminal=True,
        status_provenance_authenticated=True,
        gates_passed=True,
        expected_branch="agent/T-1",
        expected_completion_signal_id="7",
        publish_result=_publish(),
    )
    base.update(overrides)
    return CompletionInputs(**base)


def test_completes_when_all_conditions_agree() -> None:
    decision = evaluate_completion(_inputs())
    assert decision.complete is True
    assert decision.reason == "agree"


def test_unauthenticated_status_does_not_complete() -> None:
    decision = evaluate_completion(_inputs(status_provenance_authenticated=False))
    assert decision.complete is False
    assert decision.reason == "unauthenticated_status"


def test_missing_publish_result_does_not_complete() -> None:
    decision = evaluate_completion(_inputs(publish_result=None))
    assert decision.complete is False
    assert decision.reason == "no_publish_result"


def test_failed_gates_do_not_complete() -> None:
    decision = evaluate_completion(_inputs(gates_passed=False))
    assert decision.complete is False
    assert decision.reason == "gates_failed"


def test_branch_mismatch_does_not_complete() -> None:
    decision = evaluate_completion(_inputs(publish_result=_publish(branch="agent/other")))
    assert decision.complete is False
    assert decision.reason == "branch_mismatch"


def test_artifact_identifier_mismatch_does_not_complete() -> None:
    decision = evaluate_completion(_inputs(publish_result=_publish(signal_id="99")))
    assert decision.complete is False
    assert decision.reason == "artifact_mismatch"


def test_disagreement_applies_source_control_precedence() -> None:
    decision = evaluate_completion(
        _inputs(ticket_status_terminal=True, source_control_terminal=False)
    )
    assert decision.complete is False
    assert decision.precedence_applied == PRECEDENCE_SOURCE_CONTROL

    decision2 = evaluate_completion(
        _inputs(ticket_status_terminal=False, source_control_terminal=True)
    )
    assert decision2.complete is True
    assert decision2.precedence_applied == PRECEDENCE_SOURCE_CONTROL
