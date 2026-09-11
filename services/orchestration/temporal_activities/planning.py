"""Temporal activity that gathers repository context for the planning stage."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from observability import stage_span
from temporalio import activity

from services.orchestration.repo_intelligence_client import fetch_context_package
from services.repository_intelligence.context_metrics import context_accuracy_attributes
from services.repository_intelligence.normalization.serialization import render_context_summary


@dataclass
class ContextRequest:
    repo: str
    commit: str = ""
    focus: str = ""
    detail: str = ""
    execution_id: str | None = None
    task_id: str | None = None


@dataclass
class ContextResult:
    summary: str
    package: dict[str, Any] = field(default_factory=dict)


@activity.defn
async def gather_repo_context(req: ContextRequest) -> ContextResult:
    with stage_span(
        "context.gather",
        stage="context",
        execution_id=req.execution_id,
        task_id=req.task_id,
        attributes={"repo": req.repo, "commit": req.commit},
    ) as span:
        started = time.monotonic()
        package = await fetch_context_package(req.repo, req.commit, req.focus, req.detail)
        warm_latency_ms = (time.monotonic() - started) * 1000.0
        if span is not None:
            span.set_attribute("context.warm_latency_ms", round(warm_latency_ms, 2))
            for key, value in context_accuracy_attributes(package).items():
                span.set_attribute(key, value)
        return ContextResult(summary=render_context_summary(package), package=package)
