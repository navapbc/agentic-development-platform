"""Workflow mixin that parks a durable flow on a resume signal instead of retrying."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import TemporalError

from services.orchestration.suspension import (
    RESUME_SIGNAL,
    ResumeOn,
    SuspendReason,
    Suspension,
    default_resume_on,
)

PARK_DECISION_KEY_PATCH = "park-decision-key"
REPAIR_STAGE = "repair"
STALE_DECISION = "this decision does not match the park the run is waiting on"


def is_code_defect(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return False
    return not isinstance(exc, TemporalError)


class SuspendableWorkflow:
    def __init__(self) -> None:
        self._resume_requested = False
        self._resume_trigger = ""
        self._suspension: Suspension | None = None
        self._park_keys: bool | None = None
        self._park_seq: int = 0

    def _park_keys_enabled(self) -> bool:
        """Whether this run keys its parks, which runs started before the patch do not."""
        if self._park_keys is None:
            self._park_keys = workflow.patched(PARK_DECISION_KEY_PATCH)
        return self._park_keys

    def _next_park_key(self, execution_id: str, stage: str, subject: str) -> str:
        """Return the key of the next park, unique within the run through its ordinal.

        ``subject`` is the gate list of a gate park or the attempt count of a repair park.
        """
        if not self._park_keys_enabled():
            return ""
        return self._numbered_park_key(execution_id, stage, subject)

    def _numbered_park_key(self, execution_id: str, stage: str, subject: str) -> str:
        self._park_seq += 1
        return f"{execution_id}:{stage}:{subject}:{self._park_seq}"

    def _waiting_park_key(self, *, repair: bool) -> str:
        suspension = self._suspension
        if suspension is None or not suspension.park_key:
            return ""
        # The Abandon button of a limit park sends the repair decision.
        if suspension.reason == SuspendReason.LIMIT_REACHED:
            return suspension.park_key if repair else ""
        if suspension.reason != SuspendReason.AWAITING_HUMAN:
            return ""
        if (suspension.stage == REPAIR_STAGE) != repair:
            return ""
        return suspension.park_key

    def _require_waiting_park(self, park_key: str, *, repair: bool) -> None:
        if not park_key or self._waiting_park_key(repair=repair) != park_key:
            raise ValueError(STALE_DECISION)

    @workflow.signal(name=RESUME_SIGNAL)
    def resume(self, trigger: str = "") -> None:
        self._resume_trigger = trigger
        self._resume_requested = True

    @workflow.query
    def suspension_state(self) -> dict[str, str]:
        suspension = self._suspension
        if suspension is None:
            return {}
        return {
            "reason": suspension.reason.value,
            "resume_on": suspension.resume_on.value,
            "stage": suspension.stage,
            "parked_build_id": suspension.parked_build_id,
            "detail": suspension.detail,
            "park_key": suspension.park_key,
        }

    @staticmethod
    def _accepts_resume(suspension: Suspension, trigger: str) -> bool:
        if suspension.resume_on == ResumeOn.NEWER_BUILD:
            return True
        if not trigger:
            return False
        try:
            return ResumeOn(trigger) == suspension.resume_on
        except ValueError:
            return False

    async def suspend_for(
        self,
        reason: SuspendReason,
        *,
        resume_on: ResumeOn | None = None,
        stage: str = "",
        parked_build_id: str = "",
        detail: str = "",
    ) -> ResumeOn | None:
        return await self.await_resume(
            Suspension(
                reason=reason,
                resume_on=resume_on or default_resume_on(reason),
                stage=stage,
                parked_build_id=parked_build_id,
                detail=detail,
            )
        )

    async def await_resume(
        self, suspension: Suspension, *, until: Callable[[], bool] | None = None
    ) -> ResumeOn | None:
        """Park until a resume signal matches, or, when ``until`` is given, until it holds.

        With ``until`` the resume signal and its trigger are ignored, so only the condition
        ends the park.
        """
        self._suspension = suspension
        if until is not None:
            await workflow.wait_condition(until)
            self._suspension = None
            self._resume_requested = False
            self._resume_trigger = ""
            return suspension.resume_on
        while True:
            self._resume_requested = False
            self._resume_trigger = ""
            await workflow.wait_condition(lambda: self._resume_requested)
            trigger = self._resume_trigger
            if self._accepts_resume(suspension, trigger):
                break
        self._suspension = None
        self._resume_requested = False
        self._resume_trigger = ""
        if not trigger:
            return None
        try:
            return ResumeOn(trigger)
        except ValueError:
            return None
        return None
