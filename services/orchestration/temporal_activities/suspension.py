"""Temporal activities that record and clear parked-workflow suspension registry rows."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity

from services.orchestration.suspension_store import (
    PostgresSuspensionStore,
    SuspensionRecord,
)

_store: PostgresSuspensionStore | None = None


def _get_store() -> PostgresSuspensionStore:
    global _store
    if _store is None:
        _store = PostgresSuspensionStore()
    return _store


@dataclass
class RecordSuspensionRequest:
    workflow_id: str
    reason: str
    resume_on: str
    parked_build_id: str = ""
    stage: str = ""
    detail: str = ""


@dataclass
class ClearSuspensionRequest:
    workflow_id: str


@activity.defn
async def record_suspension(req: RecordSuspensionRequest) -> str:
    await _get_store().record(
        SuspensionRecord(
            workflow_id=req.workflow_id,
            reason=req.reason,
            resume_on=req.resume_on,
            parked_build_id=req.parked_build_id,
            stage=req.stage,
            detail=req.detail,
        )
    )
    return req.workflow_id


@activity.defn
async def clear_suspension(req: ClearSuspensionRequest) -> str:
    await _get_store().clear(req.workflow_id)
    return req.workflow_id
