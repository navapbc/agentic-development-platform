"""Tests for the activity that deletes a run's LiteLLM key when the run ends."""

from __future__ import annotations

from services.integration import litellm_admin
from services.integration.litellm_admin import LiteLLMAdminError
from services.orchestration.temporal_activities.model_access import (
    DeleteRunModelKeyRequest,
    delete_run_model_key,
)
from temporalio.testing import ActivityEnvironment


async def test_the_run_key_is_deleted(monkeypatch):
    deleted: list[str] = []

    async def revoke(execution_id, *, settings=None, client=None):
        deleted.append(execution_id)
        return True

    monkeypatch.setattr(litellm_admin, "revoke_run_model_key", revoke)

    result = await ActivityEnvironment().run(
        delete_run_model_key, DeleteRunModelKeyRequest(execution_id="exec-1")
    )

    assert result.deleted is True
    assert deleted == ["exec-1"]


async def test_an_unreachable_litellm_is_reported_without_failing(monkeypatch):
    async def revoke(execution_id, *, settings=None, client=None):
        raise LiteLLMAdminError("LiteLLM at http://litellm:4000 could not be reached")

    monkeypatch.setattr(litellm_admin, "revoke_run_model_key", revoke)

    result = await ActivityEnvironment().run(
        delete_run_model_key, DeleteRunModelKeyRequest(execution_id="exec-1")
    )

    assert result.deleted is False
    assert "could not be reached" in result.error
