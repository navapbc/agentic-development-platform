"""One contract that review gates and scanner gates satisfy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

PASS = "pass"
BLOCK = "block"
ADVISORY = "advisory"
CANCELLED = "cancelled"
_VERDICTS = frozenset({PASS, BLOCK, ADVISORY})

GATE_CONTRACT_VERSION = "1"

REVIEW = "review"
SCANNER = "scanner"
_KINDS = frozenset({REVIEW, SCANNER})

FAIL_CLOSED = "fail_closed"
PAUSE_FOR_HUMAN = "pause_for_human"
_ON_BLOCK = frozenset({FAIL_CLOSED, PAUSE_FOR_HUMAN})


@dataclass(frozen=True)
class GateContext:
    task_id: str
    execution_id: str
    repo: str
    ref: str
    repo_root: str = ""
    subject: str = ""
    context_summary: str = ""
    acceptance_criteria: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateNode:
    name: str
    required: bool = True
    after: tuple[str, ...] = ()
    on_block: str = FAIL_CLOSED


@dataclass(frozen=True)
class GateFinding:
    severity: str
    title: str
    detail: str = ""
    location: str = ""
    rule_id: str = ""


@dataclass(frozen=True)
class GateResult:
    name: str
    kind: str
    verdict: str
    findings: list[GateFinding] = field(default_factory=list)
    summary: str = ""
    attestation_hash: str = ""
    content_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Gate(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    async def evaluate(self, context: GateContext) -> GateResult: ...


GateFactory = Callable[[], Gate]
EvaluateFn = Callable[[GateContext], Awaitable[GateResult]]


@dataclass(frozen=True)
class _CallableGate:
    name: str
    kind: str
    _evaluate: EvaluateFn

    async def evaluate(self, context: GateContext) -> GateResult:
        return await self._evaluate(context)


def gate_from_callable(name: str, kind: str, evaluate: EvaluateFn) -> Gate:
    if kind not in _KINDS:
        raise ValueError(f"gate {name!r} kind must be one of {sorted(_KINDS)}, got {kind!r}")
    return _CallableGate(name=name, kind=kind, _evaluate=evaluate)
