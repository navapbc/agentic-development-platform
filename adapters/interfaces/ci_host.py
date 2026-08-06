"""Vendor-neutral contract for the CI operations the platform mediates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

SUCCESS = "SUCCESS"
FAILURE = "FAILURE"
UNSTABLE = "UNSTABLE"
ABORTED = "ABORTED"

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"

_FAILURE_MARKERS = ("FAIL", "ERROR", "Traceback", "AssertionError", "BUILD FAILURE")


@dataclass(frozen=True)
class BuildRef:
    repo: str
    job: str
    queue_id: int
    build_number: int
    url: str
    triggered: bool


@dataclass(frozen=True)
class BuildResult:
    repo: str
    job: str
    build_number: int
    status: str
    result: str
    url: str
    completed: bool


@dataclass(frozen=True)
class BuildLog:
    repo: str
    job: str
    build_number: int
    text: str
    failure_summary: str


def failure_summary(log_text: str, *, max_lines: int = 20) -> str:
    picked = [line for line in log_text.splitlines() if any(m in line for m in _FAILURE_MARKERS)]
    return "\n".join(picked[-max_lines:])


class CIHost(Protocol):
    async def trigger_build(
        self,
        repo: str,
        job: str,
        *,
        branch: str,
        commit_sha: str,
        execution_id: str,
        parameters: Mapping[str, str] | None = None,
    ) -> BuildRef: ...

    async def resolve_queue(self, repo: str, job: str, queue_id: int) -> BuildRef: ...

    async def get_build(self, repo: str, job: str, build_number: int) -> BuildResult: ...

    async def get_build_log(self, repo: str, job: str, build_number: int) -> BuildLog: ...
