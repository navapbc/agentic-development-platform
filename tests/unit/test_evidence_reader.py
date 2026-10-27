"""Reader coverage for every artifact type the platform routes through the evidence sink.

Each case drives the producer, persists the record it built through a sink that stands in
for the table, and resolves it by id through the reader.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from services.integration.mcp_gateway.evidence_reader import PostgresEvidenceReader
from services.review.evidence import (
    APPROVAL_COMPONENT,
    BUILD_AND_TEST_COMPONENT,
    PLAN_COMPONENT,
    PROMPT_REFERENCE_COMPONENT,
    PULL_REQUEST_SUMMARY_COMPONENT,
    EvidenceRecord,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression
from tests.unit._evidence_producers import PRODUCERS

COMPONENTS = {
    "plan": PLAN_COMPONENT,
    "pull-request-summary": PULL_REQUEST_SUMMARY_COMPONENT,
    "approval": APPROVAL_COMPONENT,
    "prompt-reference": PROMPT_REFERENCE_COMPONENT,
    "build-and-test": BUILD_AND_TEST_COMPONENT,
}


class _FakeMappings:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def first(self) -> dict[str, Any] | None:
        return self._row


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._row)


class _FakeConnection:
    """Stands in for the evidence table, evaluating the statement's own WHERE predicate.

    Matching on the compiled SQL text would accept a reader that filtered on the wrong
    column, because the bound value appears in the text either way.
    """

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    @staticmethod
    def _predicates(statement) -> list[tuple[str, Any]]:
        whereclause = statement.whereclause
        clauses = getattr(whereclause, "clauses", [whereclause])
        pairs = []
        for clause in clauses:
            if not isinstance(clause, BinaryExpression):
                raise AssertionError(f"unsupported predicate {clause!r}")
            if clause.operator is not operators.eq:
                raise AssertionError(f"unsupported operator in {clause!r}")
            pairs.append((clause.left.name, clause.right.value))
        return pairs

    async def execute(self, statement) -> _FakeResult:
        predicates = self._predicates(statement)
        for row in self._rows.values():
            if all(row.get(column) == value for column, value in predicates):
                return _FakeResult(row)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connect(self) -> _FakeConnection:
        return self._connection


def _stored_row(record: EvidenceRecord) -> dict[str, Any]:
    """Mirror the columns PostgresEvidenceSink writes for one record."""
    return {
        "artifact_id": record.artifact_id,
        "execution_id": record.execution_id,
        "content_hash": record.content_hash,
        "source_component": record.source_component,
        "classification": record.classification,
        "retention": record.retention,
        "location": record.location,
        "metadata": record.metadata or None,
    }


def _reader(records: list[EvidenceRecord]) -> PostgresEvidenceReader:
    rows = {record.artifact_id: _stored_row(record) for record in records}
    engine = cast(AsyncEngine, _FakeEngine(_FakeConnection(rows)))
    return PostgresEvidenceReader(engine)


@pytest.mark.parametrize("kind", sorted(PRODUCERS))
async def test_each_routed_artifact_resolves_by_id(kind: str, monkeypatch):
    record = await PRODUCERS[kind](monkeypatch)

    artifact = await _reader([record]).get_artifact(record.artifact_id)

    assert artifact is not None
    assert artifact["source_component"] == COMPONENTS[kind]
    assert artifact["content_hash"] == record.content_hash
    assert artifact["execution_id"] == record.execution_id
    assert artifact["classification"] == record.classification
    assert artifact["metadata"] == record.metadata


async def test_the_reader_resolves_one_artifact_out_of_every_routed_type(monkeypatch):
    records = [await producer(monkeypatch) for producer in PRODUCERS.values()]
    reader = _reader(records)

    resolved = [await reader.get_artifact(record.artifact_id) for record in records]

    assert [artifact["source_component"] for artifact in resolved] == [  # pyright: ignore[reportOptionalSubscript]
        COMPONENTS[kind] for kind in PRODUCERS
    ]


async def test_an_unknown_artifact_id_resolves_to_none(monkeypatch):
    record = await PRODUCERS["plan"](monkeypatch)

    assert await _reader([record]).get_artifact("art-missing") is None
