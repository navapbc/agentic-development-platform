"""Run a stage's gates in parallel dependency levels."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from services.orchestration.gates.contract import (
    BLOCK,
    CANCELLED,
    GateContext,
    GateNode,
    GateResult,
)

ExecuteFn = Callable[[GateNode, GateContext], Awaitable[GateResult]]


def gate_levels(specs: Sequence[GateNode]) -> list[list[GateNode]]:
    order = [spec.name for spec in specs]
    by_name = {spec.name: spec for spec in specs}
    remaining = {spec.name: {dep for dep in spec.after if dep in by_name} for spec in specs}
    placed: set[str] = set()
    levels: list[list[GateNode]] = []
    while len(placed) < len(specs):
        ready = [name for name in order if name not in placed and remaining[name] <= placed]
        if not ready:
            raise ValueError("gate stage has a cyclic after edge")
        levels.append([by_name[name] for name in ready])
        placed.update(ready)
    return levels


def _cancelled(spec: GateNode) -> GateResult:
    return GateResult(name=spec.name, kind="", verdict=CANCELLED)


def _aborts(spec: GateNode, result: GateResult) -> bool:
    return spec.required and result.verdict == BLOCK


async def run_gate_stage(
    specs: Sequence[GateNode],
    context: GateContext,
    *,
    execute: ExecuteFn,
    fail_fast: bool = False,
) -> list[GateResult]:
    results: list[GateResult] = []
    remaining_levels = gate_levels(specs)
    for index, level in enumerate(remaining_levels):
        if not fail_fast:
            results.extend(await asyncio.gather(*(execute(spec, context) for spec in level)))
            continue

        pending = {asyncio.ensure_future(execute(spec, context)): spec for spec in level}
        aborted = False
        while pending:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                spec = pending.pop(task)
                result = task.result()
                results.append(result)
                if _aborts(spec, result):
                    aborted = True
            if aborted:
                break
        if aborted:
            for task, spec in pending.items():
                task.cancel()
                results.append(_cancelled(spec))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for later in remaining_levels[index + 1 :]:
                results.extend(_cancelled(spec) for spec in later)
            break
    return results
