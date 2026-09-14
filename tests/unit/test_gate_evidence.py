"""Unit tests for the evaluate_gate activity's per-gate evidence write."""

from __future__ import annotations

import pytest
import services.orchestration.gates.config as gates_config
import services.orchestration.gates.registry as gates_registry
from services.orchestration.gates.contract import (
    PASS,
    REVIEW,
    GateContext,
    GateResult,
    gate_from_callable,
)
from services.orchestration.gates.registry import GateRegistry
from services.orchestration.temporal_activities.gates import EvaluateGateRequest, evaluate_gate

pytestmark = pytest.mark.asyncio


def _context() -> GateContext:
    return GateContext(
        task_id="T-1", execution_id="exec-1", repo="agentic-demo-service", ref="a" * 40
    )


async def test_evaluate_gate_writes_one_evidence_record(monkeypatch):
    async def evaluate(context: GateContext) -> GateResult:
        return GateResult(
            name="code-review",
            kind=REVIEW,
            verdict=PASS,
            attestation_hash="sha256:code-review",
        )

    registry = GateRegistry()
    registry.register("code-review", lambda: gate_from_callable("code-review", REVIEW, evaluate))

    monkeypatch.setattr(gates_config, "load_gates_config", lambda *a, **k: object())
    monkeypatch.setattr(gates_registry, "registry_for_config", lambda config: registry)

    persisted = []

    class _FakeSink:
        async def persist(self, record):
            persisted.append(record)
            return record.artifact_id

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _FakeSink)

    result = await evaluate_gate(EvaluateGateRequest(gate="code-review", context=_context()))

    assert result.verdict == PASS
    assert len(persisted) == 1
    assert persisted[0].source_component == "gate:code-review"
    assert persisted[0].execution_id == "exec-1"
    assert persisted[0].content_hash == "sha256:code-review"
    assert persisted[0].metadata["gate"] == "code-review"
    assert persisted[0].metadata["verdict"] == PASS


async def test_record_manifest_pins_the_gate_set(monkeypatch):
    import services.coordination.manifest as coordination_manifest
    import services.orchestration.gates.config as gates_config_mod
    import services.orchestration.temporal_activities.manifest as manifest_mod
    from services.orchestration.gates.config import parse_gates_config
    from services.orchestration.temporal_activities.manifest import (
        RecordManifestRequest,
        record_manifest,
    )

    config = parse_gates_config({"profile": "government"})
    monkeypatch.setattr(gates_config_mod, "load_gates_config", lambda *a, **k: config)

    async def _fake_bundle():
        return "0.3.0"

    async def _fake_models():
        return {"planning-model": "bedrock/claude-plan", "coding-model": "bedrock/claude-code"}

    monkeypatch.setattr(manifest_mod, "_resolve_bundle_version", _fake_bundle)
    monkeypatch.setattr(manifest_mod, "_resolve_models", _fake_models)

    recorded = []

    class _FakeStore:
        async def record(self, manifest):
            recorded.append(manifest)
            return coordination_manifest.ManifestRecord("event-1", manifest)

    monkeypatch.setattr(coordination_manifest, "PostgresManifestStore", _FakeStore)

    event_id = await record_manifest(
        RecordManifestRequest(
            execution_id="exec-1",
            task_id="AGENTDEV-1",
            repository_sha="a" * 40,
            snapshot_version=2,
        )
    )

    assert event_id == "event-1"
    assert len(recorded) == 1
    manifest = recorded[0]
    assert manifest.repository_sha == "a" * 40
    assert manifest.snapshot_version == 2
    assert manifest.policy_bundle_version == "0.3.0"
    assert manifest.resolved_models == {
        "planning-model": "bedrock/claude-plan",
        "coding-model": "bedrock/claude-code",
    }
    assert manifest.prompt_versions == {
        "code-review": "code-review/v1",
        "security-review": "security-review/v1",
        "acceptance": "acceptance-verification/v2",
    }
    assert manifest.gate_config_hash.startswith("sha256:")
    assert set(manifest.gate_versions) == {
        "code-review",
        "security-review",
        "acceptance",
        "semgrep",
        "trivy",
    }
    assert "gate_config_hash" in manifest.to_dict()



async def test_evaluate_gate_fails_closed_on_a_backend_error(monkeypatch):
    from services.orchestration.gates.contract import BLOCK, SCANNER

    async def boom(context: GateContext) -> GateResult:
        raise RuntimeError("engine entrypoint missing")

    registry = GateRegistry()
    registry.register("ai-security-review", lambda: gate_from_callable(
        "ai-security-review", SCANNER, boom))

    monkeypatch.setattr(gates_config, "load_gates_config", lambda *a, **k: object())
    monkeypatch.setattr(gates_registry, "registry_for_config", lambda config: registry)

    persisted = []

    class _FakeSink:
        async def persist(self, record):
            persisted.append(record)
            return record.artifact_id

    monkeypatch.setattr("services.review.evidence.PostgresEvidenceSink", _FakeSink)

    result = await evaluate_gate(
        EvaluateGateRequest(gate="ai-security-review", context=_context())
    )

    assert result.verdict == BLOCK
    assert result.metadata.get("backend_error") is True
    assert result.findings and result.findings[0].rule_id == "gate-backend-error"
    assert "engine entrypoint missing" in result.findings[0].detail
    assert len(persisted) == 1
