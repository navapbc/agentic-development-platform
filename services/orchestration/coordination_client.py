"""Async HTTP client for the coordination-service idempotency, sequencing, and claim RPC."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx
from scripts.config import get_settings

from services.coordination.claims import ClaimOutcome, Schedule, WorkUnit
from services.coordination.idempotency import SideEffectOutcome, SideEffectRef
from services.coordination.sequencing import SequencedAction, SequenceDecision

Effect = Callable[[], Any] | Callable[[], Awaitable[Any]]


async def _resolve(effect: Effect) -> Any:
    result = effect()
    if inspect.isawaitable(result):
        return await result
    return result


class CoordinationClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 30.0) -> None:
        self._base_url = base_url or get_settings().coordination_url
        self._timeout = timeout

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()

    async def reserve_side_effect(self, ref: SideEffectRef) -> SideEffectOutcome:
        data = await self._post(
            "/idempotency/reserve",
            {
                "task_id": ref.task_id,
                "execution_id": ref.execution_id,
                "operation": ref.operation,
                "target": ref.target,
            },
        )
        return SideEffectOutcome(**data)

    async def record_side_effect(self, ref: SideEffectRef, result: Any) -> SideEffectOutcome:
        data = await self._post(
            "/idempotency/record",
            {
                "task_id": ref.task_id,
                "execution_id": ref.execution_id,
                "operation": ref.operation,
                "target": ref.target,
                "result": result,
            },
        )
        return SideEffectOutcome(**data)

    async def run_once(self, ref: SideEffectRef, effect: Effect) -> SideEffectOutcome:
        reserved = await self.reserve_side_effect(ref)
        if reserved.replayed:
            return reserved
        result = await _resolve(effect)
        return await self.record_side_effect(ref, result)

    async def admit_sequence(self, action: SequencedAction) -> SequenceDecision:
        data = await self._post(
            "/sequence/admit",
            {
                "task_id": action.task_id,
                "execution_id": action.execution_id,
                "sequence": action.sequence,
                "idempotency_key": action.idempotency_key,
                "operation": action.operation,
            },
        )
        return SequenceDecision(**data)

    async def try_claim(self, unit: WorkUnit) -> ClaimOutcome:
        data = await self._post("/claims/try", _work_unit_body(unit))
        return ClaimOutcome(**data)

    async def next_batch(self, units: Iterable[WorkUnit]) -> Schedule:
        data = await self._post(
            "/claims/next_batch",
            {"units": [_work_unit_body(u) for u in units]},
        )
        return Schedule(
            admitted=tuple(_work_unit(u) for u in data["admitted"]),
            deferred=tuple(_work_unit(u) for u in data["deferred"]),
        )


def _work_unit_body(unit: WorkUnit) -> dict[str, Any]:
    return {
        "task_id": unit.task_id,
        "work_unit": unit.work_unit,
        "execution_id": unit.execution_id,
        "agent_id": unit.agent_id,
        "file_impact": list(unit.file_impact),
    }


def _work_unit(data: dict[str, Any]) -> WorkUnit:
    payload = dict(data)
    payload["file_impact"] = tuple(payload.get("file_impact") or ())
    return WorkUnit(**payload)
