"""Vendor-neutral contract for the git host operations the platform mediates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FileContent:
    path: str
    text: str
    ref: str


@dataclass(frozen=True)
class CodeHit:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class BranchRef:
    repo: str
    name: str
    head_sha: str
    created: bool


@dataclass(frozen=True)
class CommitRequest:
    repo: str
    branch: str
    base: str
    message: str
    files: Mapping[str, str]
    task_id: str
    execution_id: str
    author_name: str = "agent-platform"
    author_email: str = "agent-platform@users.noreply.invalid"


@dataclass(frozen=True)
class CommitResult:
    repo: str
    branch: str
    commit_sha: str
    pushed: bool
    replayed: bool


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    url: str
    head: str
    base: str
    title: str
    body: str
    draft: bool
    state: str
    created: bool


@dataclass(frozen=True)
class PRComment:
    author: str
    body: str


@dataclass(frozen=True)
class PRFeedback:
    repo: str
    number: int
    state: str
    comments: tuple[PRComment, ...] = ()


def agent_branch_name(task_id: str) -> str:
    return f"agent/{task_id}"


class GitHost(Protocol):
    async def read_file(self, repo: str, path: str, ref: str) -> FileContent: ...

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]: ...

    async def ensure_branch(self, repo: str, name: str, base: str) -> BranchRef: ...

    async def commit_and_push(self, request: CommitRequest) -> CommitResult: ...

    async def open_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...

    async def find_open_pr(self, repo: str, head: str) -> PullRequest | None: ...

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback: ...

    async def delete_branch(self, repo: str, name: str) -> bool: ...

    async def close_pr(self, repo: str, number: int) -> PullRequest: ...

    async def mark_pr_ready(self, repo: str, number: int) -> PullRequest: ...
