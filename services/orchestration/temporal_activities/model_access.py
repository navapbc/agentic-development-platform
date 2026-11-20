"""Temporal activity that deletes a run's LiteLLM virtual key when the run ends."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity


@dataclass
class DeleteRunModelKeyRequest:
    execution_id: str


@dataclass
class DeleteRunModelKeyResult:
    deleted: bool
    error: str = ""


@activity.defn
async def delete_run_model_key(req: DeleteRunModelKeyRequest) -> DeleteRunModelKeyResult:
    """Delete the run's key, reporting rather than raising when LiteLLM cannot be reached.

    A key that is already gone counts as deleted. A key this cannot delete stops working at the
    expiry it was issued with, so a failure here never fails the run.
    """
    from services.integration.litellm_admin import revoke_run_model_key

    try:
        await revoke_run_model_key(req.execution_id)
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "could not delete the LiteLLM key of run %s before its expiry (%s)",
            req.execution_id,
            exc,
        )
        return DeleteRunModelKeyResult(deleted=False, error=str(exc))
    return DeleteRunModelKeyResult(deleted=True)
