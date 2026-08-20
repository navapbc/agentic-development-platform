"""Unit tests for the parallel gate stage runner and dependency levels."""

from __future__ import annotations

import asyncio

import pytest
from services.orchestration.gates.contract import (
    PASS,
    SCANNER,
    GateContext,
    GateNode,
    GateResult,
)
from services.orchestration.gates.runner import gate_levels, run_gate_stage

pytestmark = pytest.mark.asyncio


def _context() -> GateContext:
    return GateContext(
        task_id="T-1", execution_id="exec-1", repo="agentic-demo-service", ref="a" * 40
    )


def test_independent_gates_form_one_level():
    specs = [GateNode("semgrep"), GateNode("trivy")]
    levels = gate_levels(specs)
    assert [[spec.name for spec in level] for level in levels] == [["semgrep", "trivy"]]


def test_after_edges_form_ordered_levels():
    specs = [
        GateNode("code-review"),
        GateNode("security-review"),
        GateNode("acceptance", after=("code-review", "security-review")),
    ]
    levels = gate_levels(specs)
    assert [[spec.name for spec in level] for level in levels] == [
        ["code-review", "security-review"],
        ["acceptance"],
    ]


def test_cyclic_levels_raise():
    specs = [
        GateNode("a", after=("b",)),
        GateNode("b", after=("a",)),
    ]
    with pytest.raises(ValueError):
        gate_levels(specs)


async def test_a_level_runs_concurrently():
    active = 0
    peak = 0

    async def execute(spec, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return GateResult(name=spec.name, kind=SCANNER, verdict=PASS)

    results = await run_gate_stage(
        [GateNode("semgrep"), GateNode("trivy")], _context(), execute=execute
    )

    assert peak == 2
    assert {result.name for result in results} == {"semgrep", "trivy"}


async def test_an_after_gate_starts_only_once_its_predecessor_finishes():
    events: list[tuple[str, str]] = []

    async def execute(spec, context):
        events.append(("start", spec.name))
        await asyncio.sleep(0.01)
        events.append(("end", spec.name))
        return GateResult(name=spec.name, kind=SCANNER, verdict=PASS)

    await run_gate_stage(
        [GateNode("code-review"), GateNode("acceptance", after=("code-review",))],
        _context(),
        execute=execute,
    )

    assert events.index(("start", "acceptance")) > events.index(("end", "code-review"))


async def test_a_blocking_gate_does_not_cancel_its_siblings():
    from services.orchestration.gates.contract import BLOCK

    async def execute(spec, context):
        await asyncio.sleep(0.01)
        verdict = BLOCK if spec.name == "semgrep" else PASS
        return GateResult(name=spec.name, kind=SCANNER, verdict=verdict)

    results = await run_gate_stage(
        [GateNode("semgrep"), GateNode("trivy")], _context(), execute=execute
    )

    assert len(results) == 2
    assert {result.name for result in results} == {"semgrep", "trivy"}


async def test_fail_fast_aborts_and_cancels_siblings():
    from services.orchestration.gates.contract import BLOCK, CANCELLED

    async def execute(spec, context):
        if spec.name == "semgrep":
            return GateResult(name="semgrep", kind=SCANNER, verdict=BLOCK)
        await asyncio.sleep(10)
        return GateResult(name=spec.name, kind=SCANNER, verdict=PASS)

    results = await run_gate_stage(
        [GateNode("semgrep", required=True), GateNode("trivy", required=True)],
        _context(),
        execute=execute,
        fail_fast=True,
    )

    verdicts = {result.name: result.verdict for result in results}
    assert verdicts["semgrep"] == BLOCK
    assert verdicts["trivy"] == CANCELLED


async def test_fail_fast_ignores_an_advisory_block():
    from services.orchestration.gates.contract import BLOCK

    async def execute(spec, context):
        if spec.name == "semgrep":
            return GateResult(name="semgrep", kind=SCANNER, verdict=BLOCK)
        await asyncio.sleep(0.01)
        return GateResult(name="trivy", kind=SCANNER, verdict=PASS)

    results = await run_gate_stage(
        [GateNode("semgrep", required=False), GateNode("trivy", required=True)],
        _context(),
        execute=execute,
        fail_fast=True,
    )

    verdicts = {result.name: result.verdict for result in results}
    assert verdicts["semgrep"] == BLOCK
    assert verdicts["trivy"] == PASS


async def test_fail_fast_cancels_a_later_level():
    from services.orchestration.gates.contract import BLOCK, CANCELLED

    async def execute(spec, context):
        verdict = BLOCK if spec.name == "code-review" else PASS
        return GateResult(name=spec.name, kind="review", verdict=verdict)

    results = await run_gate_stage(
        [
            GateNode("code-review", required=True),
            GateNode("acceptance", required=True, after=("code-review",)),
        ],
        _context(),
        execute=execute,
        fail_fast=True,
    )

    verdicts = {result.name: result.verdict for result in results}
    assert verdicts["code-review"] == BLOCK
    assert verdicts["acceptance"] == CANCELLED
