"""The Postgres evidence index deduplicates evidence per execution and records its tenant.

A fake engine stands in for the evidence table and its unique constraint on the execution
and the content hash, and for the tenant each execution recorded.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.review import evidence
from services.review.evidence import EvidenceRecord, ObjectStoreEvidenceSink, PostgresEvidenceSink
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.support import hermetic_settings
from tests.unit.test_object_store_evidence import _FakeS3, _settings, _writer


class _EvidenceTable:
    def __init__(self, execution_tenants: dict[str, str]) -> None:
        self.execution_tenants = execution_tenants
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.inserts: list[str] = []

    def begin(self) -> _EvidenceConnection:
        return _EvidenceConnection(self)


class _EvidenceConnection:
    def __init__(self, table: _EvidenceTable) -> None:
        self._table = table

    async def __aenter__(self) -> _EvidenceConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement) -> SimpleNamespace:
        compiled = statement.compile(dialect=postgresql.dialect())
        params = dict(compiled.params)
        if statement.is_insert:
            self._table.inserts.append(str(compiled))
            key = (params["execution_id"], params["content_hash"])
            if key in self._table.rows:
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            self._table.rows[key] = {
                "artifact_id": params["artifact_id"],
                "tenant": self._table.execution_tenants.get(params["execution_id_1"], ""),
            }
            return SimpleNamespace(scalar_one_or_none=lambda: params["artifact_id"])
        stored = self._table.rows[(params["execution_id_1"], params["content_hash_1"])]
        return SimpleNamespace(scalar_one=lambda: stored["artifact_id"])


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "get_settings", lambda: hermetic_settings())


def _record(artifact_id: str, execution_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        artifact_id=artifact_id,
        execution_id=execution_id,
        content_hash="sha256:identical",
        source_component="gate:code-review",
        classification="standard",
    )


def _index(table: _EvidenceTable) -> PostgresEvidenceSink:
    return PostgresEvidenceSink(cast(AsyncEngine, table))


async def test_the_insert_conflicts_on_the_execution_and_content_hash():
    table = _EvidenceTable({"exec-1": "alpha"})

    await _index(table).persist(_record("art-1", "exec-1"))

    (sql,) = table.inserts
    assert "ON CONFLICT (execution_id, content_hash) DO NOTHING" in sql
    assert "coalesce((SELECT agent_platform.executions.tenant" in sql


async def test_a_repeated_persist_for_one_execution_returns_the_stored_artifact_id():
    table = _EvidenceTable({"exec-1": "alpha"})
    index = _index(table)

    first = await index.persist(_record("art-1", "exec-1"))
    replayed = await index.persist(_record("art-2", "exec-1"))

    assert (first, replayed) == ("art-1", "art-1")
    assert list(table.rows) == [("exec-1", "sha256:identical")]


async def test_two_executions_with_identical_evidence_hold_a_row_each_and_one_object():
    table = _EvidenceTable({"exec-alpha": "alpha", "exec-beta": "beta"})
    client = _FakeS3()
    sink = ObjectStoreEvidenceSink(
        index=_index(table), settings=_settings(), writer=_writer(client)
    )

    alpha = await sink.persist(_record("art-alpha", "exec-alpha"))
    beta = await sink.persist(_record("art-beta", "exec-beta"))

    assert (alpha, beta) == ("art-alpha", "art-beta")
    assert {key: row["tenant"] for key, row in table.rows.items()} == {
        ("exec-alpha", "sha256:identical"): "alpha",
        ("exec-beta", "sha256:identical"): "beta",
    }
    assert list(client.objects) == [("evidence", "evidence/sha256:identical")]
