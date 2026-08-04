"""Unit tests for the suspend reason model and version-gated resume decisions."""

from __future__ import annotations

from services.orchestration.suspension import (
    ResumeOn,
    SuspendReason,
    Suspension,
    default_resume_on,
    is_newer_build,
    select_resumable,
    should_resume,
)


def test_default_resume_on_maps_every_reason():
    assert default_resume_on(SuspendReason.AWAITING_CI) == ResumeOn.CI_RESULT
    assert default_resume_on(SuspendReason.AWAITING_REVIEW) == ResumeOn.HUMAN_APPROVAL
    assert default_resume_on(SuspendReason.AWAITING_FIX) == ResumeOn.NEWER_BUILD
    assert default_resume_on(SuspendReason.AWAITING_BUDGET) == ResumeOn.BUDGET_RAISED
    assert default_resume_on(SuspendReason.AWAITING_DEPENDENCY) == ResumeOn.DEPENDENCY_READY


def test_is_newer_build_requires_strictly_greater_version():
    assert is_newer_build("1.4.2", "1.4.1") is True
    assert is_newer_build("2", "1") is True
    assert is_newer_build("1.4.1", "1.4.1") is False
    assert is_newer_build("1.4.0", "1.4.1") is False


def test_is_newer_build_is_conservative_on_unparseable_ids():
    assert is_newer_build("dev", "1.4.1") is False
    assert is_newer_build("1.4.2", "dev") is False
    assert is_newer_build("dev", "dev") is False


def test_should_resume_awaiting_fix_uses_build_version_not_trigger():
    parked = Suspension(
        reason=SuspendReason.AWAITING_FIX,
        resume_on=ResumeOn.NEWER_BUILD,
        parked_build_id="1.4.1",
    )
    assert should_resume(parked, current_build_id="1.4.2") is True
    assert should_resume(parked, current_build_id="1.4.1") is False
    assert should_resume(parked, current_build_id="1.4.1", trigger=ResumeOn.NEWER_BUILD) is False


def test_should_resume_signalled_reasons_match_trigger():
    parked = Suspension(
        reason=SuspendReason.AWAITING_CI,
        resume_on=ResumeOn.CI_RESULT,
    )
    assert should_resume(parked, trigger=ResumeOn.CI_RESULT) is True
    assert should_resume(parked, trigger=ResumeOn.HUMAN_APPROVAL) is False
    assert should_resume(parked) is False


def test_select_resumable_returns_only_older_build_fixes():
    fix_old = Suspension(
        reason=SuspendReason.AWAITING_FIX,
        resume_on=ResumeOn.NEWER_BUILD,
        parked_build_id="1.4.1",
    )
    fix_current = Suspension(
        reason=SuspendReason.AWAITING_FIX,
        resume_on=ResumeOn.NEWER_BUILD,
        parked_build_id="1.4.2",
    )
    ci_wait = Suspension(
        reason=SuspendReason.AWAITING_CI,
        resume_on=ResumeOn.CI_RESULT,
        parked_build_id="1.4.1",
    )

    resumable = select_resumable([fix_old, fix_current, ci_wait], current_build_id="1.4.2")

    assert resumable == [fix_old]
