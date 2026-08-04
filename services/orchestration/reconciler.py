"""Reconciler that finds parked workflows and resumes the ones a newer build can retry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from services.orchestration.suspension import ResumeOn, is_newer_build
from services.orchestration.suspension_store import SuspensionRecord, SuspensionStore


@dataclass(frozen=True)
class ResumePlan:
    workflow_id: str
    trigger: ResumeOn


def plan_build_resumes(
    records: list[SuspensionRecord], *, current_build_id: str
) -> list[ResumePlan]:
    plans: list[ResumePlan] = []
    for record in records:
        if record.resume_on != ResumeOn.NEWER_BUILD.value:
            continue
        if is_newer_build(current_build_id, record.parked_build_id):
            plans.append(ResumePlan(workflow_id=record.workflow_id, trigger=ResumeOn.NEWER_BUILD))
    return plans


async def reconcile(
    store: SuspensionStore,
    send_resume: Callable[[str, ResumeOn], Awaitable[None]],
    *,
    current_build_id: str,
) -> list[ResumePlan]:
    records = await store.list_open()
    plans = plan_build_resumes(records, current_build_id=current_build_id)
    for plan in plans:
        await send_resume(plan.workflow_id, plan.trigger)
    return plans
