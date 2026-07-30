"""Orphan-artifact registration tests: deterministic ids, idempotent writes, provenance."""

from __future__ import annotations

from services.coordination.orphans import (
    BRANCH,
    InMemoryOrphanRegistry,
    OrphanArtifact,
    orphan_id_for,
)


def test_orphan_id_is_deterministic_per_type_id_and_repo():
    a = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service")
    b = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service")
    c = orphan_id_for(BRANCH, "feature/y", "agentic-demo-service")
    assert a == b
    assert a != c


async def test_register_stores_the_artifact():
    registry = InMemoryOrphanRegistry()
    orphan_id = await registry.register(
        OrphanArtifact(
            artifact_type=BRANCH,
            external_id="feature/x",
            repository="agentic-demo-service",
            owning_workflow="wf-exec-1",
        )
    )
    assert orphan_id == orphan_id_for(BRANCH, "feature/x", "agentic-demo-service")
    assert registry.records[orphan_id].owning_workflow == "wf-exec-1"


async def test_register_is_idempotent():
    registry = InMemoryOrphanRegistry()
    artifact = OrphanArtifact(
        artifact_type=BRANCH, external_id="feature/x", repository="agentic-demo-service"
    )
    first = await registry.register(artifact)
    second = await registry.register(
        OrphanArtifact(
            artifact_type=BRANCH,
            external_id="feature/x",
            repository="agentic-demo-service",
            owning_workflow="wf-later",
        )
    )
    assert first == second
    assert len(registry.records) == 1
    assert registry.records[first].owning_workflow == ""
