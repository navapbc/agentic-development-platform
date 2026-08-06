"""Platform Jenkins mediator: allowlist-guarded facade routing triggers through coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from adapters.interfaces.ci_host import BuildLog, BuildRef, BuildResult, CIHost
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
class JenkinsMediator:
    backend: CIHost
    ledger: SideEffectLedger
    gate: SequenceGate
    allowed_repos: frozenset[str]

    @classmethod
    def from_settings(
        cls,
        backend: CIHost,
        ledger: SideEffectLedger,
        gate: SequenceGate,
        settings: Settings | None = None,
    ) -> JenkinsMediator:
        resolved = settings or get_settings()
        return cls(
            backend=backend,
            ledger=ledger,
            gate=gate,
            allowed_repos=frozenset(resolved.allowed_repos()),
        )

    def _guard(self, repo: str) -> None:
        if repo not in self.allowed_repos:
            raise RepoNotAllowedError(repo)

    async def trigger_build(
        self,
        repo: str,
        task_id: str,
        execution_id: str,
        job: str,
        *,
        branch: str,
        commit_sha: str,
        sequence: int,
        parameters: Mapping[str, str] | None = None,
    ) -> BuildRef:
        self._guard(repo)
        action = SequencedAction(task_id, execution_id, sequence, operation="jenkins.trigger")
        ref = SideEffectRef(task_id, execution_id, "jenkins.trigger", f"{repo}:{job}:{commit_sha}")

        async def effect() -> dict[str, Any]:
            return asdict(
                await self.backend.trigger_build(
                    repo,
                    job,
                    branch=branch,
                    commit_sha=commit_sha,
                    execution_id=execution_id,
                    parameters=parameters,
                )
            )

        return BuildRef(**await self._gated(action, ref, effect))

    async def resolve_queue(self, repo: str, job: str, queue_id: int) -> BuildRef:
        self._guard(repo)
        return await self.backend.resolve_queue(repo, job, queue_id)

    async def get_build(self, repo: str, job: str, build_number: int) -> BuildResult:
        self._guard(repo)
        return await self.backend.get_build(repo, job, build_number)

    async def get_build_log(self, repo: str, job: str, build_number: int) -> BuildLog:
        self._guard(repo)
        return await self.backend.get_build_log(repo, job, build_number)

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
