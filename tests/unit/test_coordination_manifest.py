"""Determinism-manifest tests: first audit event pins inputs and fills execution columns."""

from __future__ import annotations

from services.coordination.manifest import (
    DeterminismManifest,
    InMemoryManifestStore,
    manifest_event_id,
)


def _manifest(**overrides) -> DeterminismManifest:
    values = {
        "execution_id": "exec-1",
        "task_id": "AGENTDEV-1",
        "snapshot_version": 3,
        "repository_sha": "a1b2c3d4",
        "policy_bundle_version": "policy/v2",
        "prompt_versions": {"plan-review": "plan-review/v1"},
        "resolved_models": {"classification-summary": "bedrock/anthropic.claude-3"},
        "gate_versions": {"code-review": "1", "semgrep": "1"},
        "gate_config_hash": "sha256:abc123",
    }
    values.update(overrides)
    return DeterminismManifest(**values)


def test_manifest_event_id_is_deterministic_per_execution():
    assert manifest_event_id("exec-1") == manifest_event_id("exec-1")
    assert manifest_event_id("exec-1") != manifest_event_id("exec-2")


def test_to_dict_carries_every_manifest_input():
    payload = _manifest().to_dict()
    assert payload == {
        "snapshot_version": 3,
        "repository_sha": "a1b2c3d4",
        "policy_bundle_version": "policy/v2",
        "prompt_versions": {"plan-review": "plan-review/v1"},
        "resolved_models": {"classification-summary": "bedrock/anthropic.claude-3"},
        "gate_versions": {"code-review": "1", "semgrep": "1"},
        "gate_config_hash": "sha256:abc123",
    }


async def test_record_writes_the_gate_set_into_the_audit_event():
    store = InMemoryManifestStore()
    record = await store.record(_manifest())
    correlation = record.manifest.to_dict()
    assert correlation["gate_versions"] == {"code-review": "1", "semgrep": "1"}
    assert correlation["gate_config_hash"] == "sha256:abc123"


async def test_record_populates_execution_manifest_columns():
    store = InMemoryManifestStore()
    await store.record(_manifest())
    columns = store.executions["exec-1"]
    assert columns == {
        "repository_sha": "a1b2c3d4",
        "policy_bundle_version": "policy/v2",
        "prompt_versions": {"plan-review": "plan-review/v1"},
        "resolved_models": {"classification-summary": "bedrock/anthropic.claude-3"},
    }


async def test_record_writes_the_manifest_audit_event():
    store = InMemoryManifestStore()
    record = await store.record(_manifest())
    assert record.event_id == manifest_event_id("exec-1")
    assert len(store.events) == 1
    assert store.events[0].event_id == record.event_id
    assert store.events[0].manifest.snapshot_version == 3


async def test_recording_twice_does_not_duplicate_the_first_event():
    store = InMemoryManifestStore()
    await store.record(_manifest())
    await store.record(_manifest())
    assert len(store.events) == 1


async def test_empty_optional_inputs_persist_as_null():
    store = InMemoryManifestStore()
    await store.record(
        DeterminismManifest(execution_id="exec-9", task_id="AGENTDEV-9", snapshot_version=1)
    )
    columns = store.executions["exec-9"]
    assert columns == {
        "repository_sha": None,
        "policy_bundle_version": None,
        "prompt_versions": None,
        "resolved_models": None,
    }
