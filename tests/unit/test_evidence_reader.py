"""Reader coverage for every artifact type the platform routes through the evidence sink.

Each case drives the producer, persists the record it built through a sink that stands in
for the table, and resolves it by id through the reader. The reader serves one tenant, so an
artifact of another tenant's execution resolves as absent.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from services.integration.mcp_gateway.evidence_reader import (
    SECURITY_COMPONENT,
    PostgresEvidenceReader,
)
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
from sqlalchemy.sql.selectable import Join
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

    def first(self) -> SimpleNamespace | None:
        return SimpleNamespace(**self._row) if self._row is not None else None


class _FakeConnection:
    """Stands in for the evidence table joined to executions, evaluating the WHERE predicate.

    Matching on the compiled SQL text would accept a reader that filtered on the wrong
    column, because the bound value appears in the text either way. Each row carries the
    tenant of its execution, which the join supplies in the database.
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

    @staticmethod
    def _joins_executions(statement) -> bool:
        return any(
            isinstance(source, Join) and source.right.name == "executions"  # pyright: ignore[reportAttributeAccessIssue]
            for source in statement.get_final_froms()
        )

    async def execute(self, statement) -> _FakeResult:
        if not self._joins_executions(statement):
            raise AssertionError("the reader must join executions to read the tenant")
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


def _stored_row(record: EvidenceRecord, tenant: str | None) -> dict[str, Any]:
    """Mirror the columns PostgresEvidenceSink writes for one record, plus its tenant."""
    return {
        "tenant": tenant,
        "artifact_id": record.artifact_id,
        "execution_id": record.execution_id,
        "content_hash": record.content_hash,
        "source_component": record.source_component,
        "classification": record.classification,
        "retention": record.retention,
        "location": record.location,
        "metadata": record.metadata or None,
    }


def _reader(
    records: list[EvidenceRecord], tenant: str = "adp", owner: str | None = "adp"
) -> PostgresEvidenceReader:
    rows = {record.artifact_id: _stored_row(record, owner) for record in records}
    engine = cast(AsyncEngine, _FakeEngine(_FakeConnection(rows)))
    return PostgresEvidenceReader(tenant, engine)


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


async def test_an_artifact_of_another_tenant_resolves_to_none(monkeypatch):
    record = await PRODUCERS["plan"](monkeypatch)

    assert (
        await _reader([record], tenant="beta", owner="adp").get_artifact(record.artifact_id) is None
    )


async def test_security_findings_resolve_only_for_the_reader_tenant(monkeypatch):
    record = await PRODUCERS["plan"](monkeypatch)
    findings = replace(
        record,
        artifact_id="art-security",
        source_component=SECURITY_COMPONENT,
        metadata={"finding_count": 1},
    )

    own = await _reader([findings], tenant="adp", owner="adp").get_security_findings(
        findings.execution_id
    )
    other = await _reader([findings], tenant="beta", owner="adp").get_security_findings(
        findings.execution_id
    )

    assert own == {"finding_count": 1}
    assert other is None


async def test_the_evidence_of_an_execution_recorded_without_a_tenant_resolves_to_none(
    monkeypatch,
):
    record = await PRODUCERS["plan"](monkeypatch)
    findings = replace(
        record,
        artifact_id="art-security",
        source_component=SECURITY_COMPONENT,
        metadata={"finding_count": 1},
    )
    reader = _reader([record, findings], tenant="adp", owner=None)

    assert await reader.get_artifact(record.artifact_id) is None
    assert await reader.get_security_findings(findings.execution_id) is None


def test_the_reader_refuses_to_serve_no_tenant():
    with pytest.raises(ValueError, match="needs the tenant"):
        PostgresEvidenceReader("")
