"""Platform GHES mediator: allowlist-guarded facade routing writes through coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from adapters.interfaces.git_host import (
    BranchRef,
    CodeHit,
    CommitRequest,
    CommitResult,
    FileContent,
    GitHost,
    PRFeedback,
    PullRequest,
    agent_branch_name,
)
from scripts.config import Settings, get_settings

from services.coordination.idempotency import SideEffectLedger, SideEffectRef
from services.coordination.sequencing import (
    ADMITTED,
    SequencedAction,
    SequenceDecision,
    SequenceGate,
)


class RepoNotAllowedError(Exception):
    def __init__(self, repo: str) -> None:
        super().__init__(f"repository not allowlisted: {repo}")
        self.repo = repo


class StaleActionError(Exception):
    def __init__(self, decision: SequenceDecision) -> None:
        super().__init__(
            f"action discarded by sequence gate: {decision.task_id} "
            f"sequence {decision.sequence} not ahead of {decision.current_sequence}"
        )
        self.decision = decision


@dataclass
class GitHubMediator:
    backend: GitHost
    ledger: SideEffectLedger
    gate: SequenceGate
    allowed_repos: frozenset[str]

    @classmethod
    def from_settings(
        cls,
        backend: GitHost,
        ledger: SideEffectLedger,
        gate: SequenceGate,
        settings: Settings | None = None,
    ) -> GitHubMediator:
        from services.integration.source_control import source_control_repos

        resolved = settings or get_settings()
        return cls(
            backend=backend,
            ledger=ledger,
            gate=gate,
            allowed_repos=frozenset(source_control_repos(resolved)),
        )

    def _guard(self, repo: str) -> None:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)

    async def read_file(self, repo: str, path: str, ref: str) -> FileContent:
        self._guard(repo)
        return await self.backend.read_file(repo, path, ref)

    async def search_code(self, repo: str, query: str, ref: str = "") -> list[CodeHit]:
        self._guard(repo)
        return await self.backend.search_code(repo, query, ref)

    async def read_pr_feedback(self, repo: str, number: int) -> PRFeedback:
        self._guard(repo)
        return await self.backend.read_pr_feedback(repo, number)

    async def find_open_pr(self, repo: str, task_id: str) -> PullRequest | None:
        self._guard(repo)
        return await self.backend.find_open_pr(repo, agent_branch_name(task_id))

    async def ensure_agent_branch(
        self,
        repo: str,
        task_id: str,
        execution_id: str,
        base: str,
        *,
        sequence: int,
    ) -> BranchRef:
        self._guard(repo)
        name = agent_branch_name(task_id)
        action = SequencedAction(task_id, execution_id, sequence, operation="github.ensure_branch")
        ref = SideEffectRef(task_id, execution_id, "github.ensure_branch", f"{repo}:{name}")

        async def effect() -> dict[str, Any]:
            return asdict(await self.backend.ensure_branch(repo, name, base))

        return BranchRef(**await self._gated(action, ref, effect))

    async def commit_and_push(self, request: CommitRequest, *, sequence: int) -> CommitResult:
        self._guard(request.repo)
        action = SequencedAction(
            request.task_id, request.execution_id, sequence, operation="github.commit"
        )
        ref = SideEffectRef(
            request.task_id,
            request.execution_id,
            "github.commit",
            f"{request.repo}:{request.branch}",
        )

        async def effect() -> dict[str, Any]:
            return asdict(await self.backend.commit_and_push(request))

        return CommitResult(**await self._gated(action, ref, effect))

    async def open_draft_pr(
        self,
        repo: str,
        task_id: str,
        execution_id: str,
        head: str,
        base: str,
        title: str,
        body: str,
        *,
        sequence: int,
    ) -> PullRequest:
        self._guard(repo)
        action = SequencedAction(task_id, execution_id, sequence, operation="github.open_pr")
        ref = SideEffectRef(task_id, execution_id, "github.open_pr", f"{repo}:{head}")

        async def effect() -> dict[str, Any]:
            return asdict(await self.backend.open_draft_pr(repo, head, base, title, body))

        return PullRequest(**await self._gated(action, ref, effect))

    async def mark_pr_ready(
        self, repo: str, task_id: str, execution_id: str, number: int, *, sequence: int
    ) -> PullRequest:
        self._guard(repo)
        action = SequencedAction(task_id, execution_id, sequence, operation="github.mark_ready")
        ref = SideEffectRef(task_id, execution_id, "github.mark_ready", f"{repo}#{number}")

        async def effect() -> dict[str, Any]:
            return asdict(await self.backend.mark_pr_ready(repo, number))

        return PullRequest(**await self._gated(action, ref, effect))

    async def record_promotion_approval(
        self,
        repo: str,
        task_id: str,
        execution_id: str,
        number: int,
        *,
        approved: bool,
        approver: str,
        sequence: int,
    ) -> dict[str, Any]:
        self._guard(repo)
        action = SequencedAction(
            task_id, execution_id, sequence, operation="github.promotion_approval"
        )
        ref = SideEffectRef(task_id, execution_id, "github.promotion_approval", f"{repo}#{number}")

        async def effect() -> dict[str, Any]:
            return {"number": number, "approved": approved, "approver": approver}

        return await self._gated(action, ref, effect)

    async def _gated(
        self,
        action: SequencedAction,
        ref: SideEffectRef,
        effect: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        reserved = await self.ledger.reserve(ref)
        if reserved.replayed:
            return reserved.result
        decision = await self.gate.admit(action)
        if decision.outcome != ADMITTED:
            raise StaleActionError(decision)
        result = await effect()
        recorded = await self.ledger.record(ref, result)
        return recorded.result
