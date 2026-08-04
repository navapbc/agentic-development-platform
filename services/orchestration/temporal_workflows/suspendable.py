"""Workflow mixin that parks a durable flow on a resume signal instead of retrying."""

from __future__ import annotations

import asyncio

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


def is_code_defect(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return False
    return not isinstance(exc, TemporalError)


class SuspendableWorkflow:
    def __init__(self) -> None:
        self._resume_requested = False
        self._resume_trigger = ""
        self._suspension: Suspension | None = None

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

    async def await_resume(self, suspension: Suspension) -> ResumeOn | None:
        self._suspension = suspension
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
