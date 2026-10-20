"""Reader coverage for every artifact type the platform routes through the evidence sink."""

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
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine


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
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement) -> _FakeResult:
        sql = str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        for artifact_id, row in self._rows.items():
            if f"'{artifact_id}'" in sql:
                return _FakeResult(row)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connect(self) -> _FakeConnection:
        return self._connection


def _row(artifact_id: str, source_component: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "execution_id": "exec-1",
        "content_hash": f"sha256:{artifact_id}",
        "source_component": source_component,
        "classification": "standard",
        "retention": "365",
        "location": None,
        "metadata": metadata,
    }


_ARTIFACTS = {
    "plan": _row("art-plan", PLAN_COMPONENT, {"task_id": "T-1", "verdict": "pass"}),
    "summary": _row("art-summary", PULL_REQUEST_SUMMARY_COMPONENT, {"number": 7}),
    "approval": _row("art-approval", APPROVAL_COMPONENT, {"approved": True}),
    "prompt": _row("art-prompt", PROMPT_REFERENCE_COMPONENT, {"prompt_version": "v2"}),
    "checks": _row("art-checks", BUILD_AND_TEST_COMPONENT, {"passed": True}),
}


def _reader() -> PostgresEvidenceReader:
    rows = {row["artifact_id"]: row for row in _ARTIFACTS.values()}
    engine = cast(AsyncEngine, _FakeEngine(_FakeConnection(rows)))
    return PostgresEvidenceReader(engine)


@pytest.mark.parametrize("kind", sorted(_ARTIFACTS))
async def test_each_routed_artifact_resolves_by_id(kind: str):
    expected = _ARTIFACTS[kind]

    artifact = await _reader().get_artifact(expected["artifact_id"])

    assert artifact is not None
    assert artifact["source_component"] == expected["source_component"]
    assert artifact["content_hash"] == expected["content_hash"]
    assert artifact["metadata"] == expected["metadata"]


async def test_an_unknown_artifact_id_resolves_to_none():
    assert await _reader().get_artifact("art-missing") is None
