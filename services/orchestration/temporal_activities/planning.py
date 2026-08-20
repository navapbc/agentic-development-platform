"""Temporal activity that gathers repository context for the planning stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from observability import stage_span
from temporalio import activity

from services.orchestration.repo_intelligence_client import fetch_context_package
from services.repository_intelligence.normalization.serialization import render_context_summary


@dataclass
class ContextRequest:
    repo: str
    commit: str = ""
    focus: str = ""


@dataclass
class ContextResult:
    summary: str
    package: dict[str, Any] = field(default_factory=dict)


@activity.defn
async def gather_repo_context(req: ContextRequest) -> ContextResult:
    with stage_span(
        "context.gather",
        stage="context",
        attributes={"repo": req.repo, "commit": req.commit},
    ):
        package = await fetch_context_package(req.repo, req.commit, req.focus)
        return ContextResult(summary=render_context_summary(package), package=package)
