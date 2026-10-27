"""Host-run workflow status resolves the published Temporal address."""

from __future__ import annotations

import pytest
from scripts.config import get_settings
from scripts.workflow_status import _list


class _FakeClient:
    def list_workflows(self):
        async def _none():
            return
            yield  # pragma: no cover

        return _none()


@pytest.fixture(autouse=True)
def on_the_host(monkeypatch):
    monkeypatch.delenv("PLATFORM_IN_CONTAINER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_workflow_status_connects_to_the_published_temporal_port(monkeypatch):
    monkeypatch.setenv("TEMPORAL_HOST", "temporal:7233")
    monkeypatch.setenv("TEMPORAL_HOST_PORT", "17233")
    recorded: dict[str, str] = {}

    class _Client:
        @staticmethod
        async def connect(address: str, namespace: str = "") -> _FakeClient:
            recorded["address"] = address
            return _FakeClient()

    monkeypatch.setattr("temporalio.client.Client", _Client)

    assert await _list() == 0
    assert recorded["address"] == "localhost:17233"
