from __future__ import annotations

from typing import Any

import pytest
import temporalio.client
from scripts import demo_draft_pr
from tests.support import hermetic_settings


class _Handle:
    id = "draft-pr-demo"

    async def result(self) -> Any:
        from services.orchestration.temporal_workflows.draft_pull_request import DRAFT_PR_OPEN

        class _Result:
            status = DRAFT_PR_OPEN
            branch = "agent/demo"
            pr_number = 1
            pr_url = "https://example.test/pr/1"
            linkback = "ADEV-1"

        return _Result()


class _Client:
    def __init__(self) -> None:
        self.started: list[Any] = []

    async def start_workflow(self, run: Any, arg: Any, **kwargs: Any) -> _Handle:
        self.started.append(arg)
        return _Handle()


@pytest.mark.asyncio
async def test_demo_run_carries_the_configured_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()

    async def _connect(address: str, **kwargs: Any) -> _Client:
        return client

    monkeypatch.setattr(temporalio.client.Client, "connect", _connect)

    async def _no_tracking(handle: Any) -> None:
        return None

    monkeypatch.setattr(demo_draft_pr, "_track_stages", _no_tracking)
    monkeypatch.setattr(demo_draft_pr, "acceptance_field", lambda settings: "")
    settings = hermetic_settings(
        implementation_max_llm_calls=640,
        implementation_max_tokens=7_500_000,
        implementation_max_cost_usd=42.5,
    )

    code = await demo_draft_pr._run_via_temporal(
        settings,
        repo="agentic-demo-service",
        base="main",
        base_sha="abc123",
        task_id="ADEV-1",
        summary="Add a widget",
        raw_issue={},
    )

    assert code == 0
    arg = client.started[0]
    assert (arg.max_llm_calls, arg.max_tokens, arg.max_cost_usd) == (640, 7_500_000, 42.5)
