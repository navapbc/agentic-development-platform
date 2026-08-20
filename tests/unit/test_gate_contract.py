"""Unit tests for the gate contract, registry, and built-in gate mappings."""

from __future__ import annotations

import pytest
from adapters.interfaces.security_scanner import ScanFinding
from services.orchestration.gates.builtin import review_gate, scanner_gate
from services.orchestration.gates.contract import (
    BLOCK,
    PASS,
    REVIEW,
    SCANNER,
    GateContext,
    GateResult,
    gate_from_callable,
)
from services.orchestration.gates.registry import (
    DuplicateGateError,
    GateNotFoundError,
    GateRegistry,
    builtin_registry,
)
from services.orchestration.temporal_activities.review import ReviewOutcome

pytestmark = pytest.mark.asyncio


def _context(**overrides) -> GateContext:
    base = {
        "task_id": "T-1",
        "execution_id": "exec-1",
        "repo": "agentic-demo-service",
        "ref": "a" * 40,
        "repo_root": "/tmp/ws",
        "subject": "a diff",
    }
    base.update(overrides)
    return GateContext(**base)


async def test_registry_resolves_registered_gate():
    registry = GateRegistry()

    async def evaluate(context: GateContext) -> GateResult:
        return GateResult(name="fake", kind=REVIEW, verdict=PASS)

    registry.register("fake", lambda: gate_from_callable("fake", REVIEW, evaluate))

    assert registry.has("fake")
    assert registry.names() == frozenset({"fake"})
    gate = registry.resolve("fake")
    result = await gate.evaluate(_context())
    assert result.verdict == PASS


def test_registry_rejects_duplicate_and_unknown():
    registry = GateRegistry()
    registry.register("fake", lambda: gate_from_callable("fake", REVIEW, _never))
    with pytest.raises(DuplicateGateError):
        registry.register("fake", lambda: gate_from_callable("fake", REVIEW, _never))
    with pytest.raises(GateNotFoundError):
        registry.resolve("missing")


async def _never(context: GateContext) -> GateResult:
    raise AssertionError("should not run")


async def test_review_gate_maps_a_blocking_outcome():
    async def fake_review(req) -> ReviewOutcome:
        return ReviewOutcome(
            verdict=BLOCK,
            attestation_hash="sha256:code",
            prompt_version="code-review/v1",
            summary="reviewed",
            findings=["dangerous call"],
        )

    gate = review_gate("code-review", fake_review)
    result = await gate.evaluate(_context())

    assert result.kind == REVIEW
    assert result.verdict == BLOCK
    assert result.summary == "reviewed"
    assert result.attestation_hash == "sha256:code"
    assert [finding.title for finding in result.findings] == ["dangerous call"]


async def test_scanner_gate_blocks_on_a_high_finding():
    class _Scanner:
        async def scan(self, target):
            return [
                ScanFinding(
                    finding_id="f1",
                    scanner="semgrep",
                    kind="static",
                    severity="high",
                    title="SQL injection",
                    location="app/db.py:20",
                    rule_id="python.sql-injection",
                ),
                ScanFinding(
                    finding_id="f2",
                    scanner="semgrep",
                    kind="static",
                    severity="low",
                    title="style nit",
                ),
            ]

    gate = scanner_gate("semgrep", lambda: _Scanner())
    result = await gate.evaluate(_context())

    assert result.kind == SCANNER
    assert result.verdict == BLOCK
    assert [finding.rule_id for finding in result.findings] == ["python.sql-injection", ""]


async def test_scanner_gate_passes_below_threshold():
    class _Scanner:
        async def scan(self, target):
            return [
                ScanFinding(
                    finding_id="f1",
                    scanner="trivy",
                    kind="dependency",
                    severity="low",
                    title="minor",
                )
            ]

    gate = scanner_gate("trivy", lambda: _Scanner())
    result = await gate.evaluate(_context())

    assert result.verdict == PASS


def test_builtin_registry_exposes_the_built_in_gates():
    registry = builtin_registry()

    assert registry.names() == frozenset(
        {
            "plan-review",
            "code-review",
            "security-review",
            "acceptance",
            "semgrep",
            "trivy",
        }
    )
    assert registry.resolve("code-review").kind == REVIEW
    assert registry.resolve("semgrep").kind == SCANNER
