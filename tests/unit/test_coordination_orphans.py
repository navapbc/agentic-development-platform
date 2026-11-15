"""Orphan-artifact registration tests: deterministic ids, idempotent writes, provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from services.coordination.orphans import (
    BRANCH,
    InMemoryOrphanRegistry,
    OrphanArtifact,
    PostgresOrphanRegistry,
    orphan_id_for,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine


def test_orphan_id_is_deterministic_per_type_id_and_repo():
    a = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service")
    b = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service")
    c = orphan_id_for(BRANCH, "feature/y", "agentic-demo-service")
    assert a == b
    assert a != c


def test_platform_tenant_id_is_unchanged():
    assert orphan_id_for(BRANCH, "feature/x", "agentic-demo-service", "") == orphan_id_for(
        BRANCH, "feature/x", "agentic-demo-service"
    )


def test_named_tenant_id_differs_from_platform_and_other_tenants():
    platform = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service")
    alpha = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service", "alpha")
    beta = orphan_id_for(BRANCH, "feature/x", "agentic-demo-service", "beta")
    assert alpha != platform
    assert beta != platform
    assert alpha != beta


async def test_register_carries_tenant_and_separates_shared_repo_orphans():
    registry = InMemoryOrphanRegistry()
    alpha_id = await registry.register(
        OrphanArtifact(
            artifact_type=BRANCH,
            external_id="agentic-demo-service:agent/alpha/AGENTDEV-1",
            repository="agentic-demo-service",
            tenant="alpha",
        )
    )
    beta_id = await registry.register(
        OrphanArtifact(
            artifact_type=BRANCH,
            external_id="agentic-demo-service:agent/beta/AGENTDEV-1",
            repository="agentic-demo-service",
            tenant="beta",
        )
    )
    assert alpha_id != beta_id
    assert registry.records[alpha_id].tenant == "alpha"
    assert registry.records[beta_id].tenant == "beta"


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


class _Connection:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self) -> _Connection:
        return self.connection


async def test_the_reapable_orphans_of_a_tenant_exclude_orphans_recorded_without_a_tenant():
    engine = _Engine()

    await PostgresOrphanRegistry(cast(AsyncEngine, engine)).list_reapable(
        datetime(2026, 8, 1, tzinfo=UTC), "adp"
    )

    sql = str(
        engine.connection.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "orphan_artifacts.tenant = 'adp'" in sql
    assert "orphan_artifacts.tenant = ''" not in sql
    assert " OR " not in sql
