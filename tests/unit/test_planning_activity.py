"""Unit tests for the planning-stage repo-context activity."""

from __future__ import annotations

import asyncio

from services.orchestration.temporal_activities import planning
from services.orchestration.temporal_activities.planning import (
    ContextRequest,
    gather_repo_context,
)


def test_gather_repo_context_renders_summary(monkeypatch):
    async def fake_fetch(repo, commit, focus):
        return {
            "repository": repo,
            "indexed_commit": commit,
            "focus": focus,
            "symbols": [],
        }

    monkeypatch.setattr(planning, "fetch_context_package", fake_fetch)

    result = asyncio.run(
        gather_repo_context(ContextRequest("agentic-demo-service", "0744a6e", "health"))
    )

    assert "Repository: agentic-demo-service @ 0744a6e" in result.summary
    assert "Focus: health" in result.summary
    assert result.package["focus"] == "health"
