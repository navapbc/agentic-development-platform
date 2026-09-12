"""Unit tests for the platform touch-set store."""

from __future__ import annotations

from collections import namedtuple

from services.orchestration.file_impact_store import FileImpactStore


class _FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class _FakeConn:
    def __init__(self, captured: list, row) -> None:
        self._captured = captured
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, stmt):
        self._captured.append(stmt)
        return _FakeResult(self._row)


class _FakeEngine:
    def __init__(self, row=None) -> None:
        self.captured: list = []
        self._row = row

    def begin(self):
        return _FakeConn(self.captured, self._row)

    def connect(self):
        return _FakeConn(self.captured, self._row)


def _store(engine: _FakeEngine) -> FileImpactStore:
    store = FileImpactStore()
    store._engine = engine
    return store


async def test_write_dedupes_and_drops_empty_paths():
    engine = _FakeEngine()
    store = _store(engine)
    await store.write("T1", ["a.ts", "a.ts", "b.ts", ""], "e1")
    assert len(engine.captured) == 1
    params = engine.captured[0].compile().params
    assert params["task_id"] == "T1"
    assert params["execution_id"] == "e1"
    assert params["paths"] == ["a.ts", "b.ts"]


async def test_read_parses_paths():
    Row = namedtuple("Row", ["paths"])
    store = _store(_FakeEngine(row=Row(paths=["a.ts", "b.ts"])))
    assert await store.read("T1") == ("a.ts", "b.ts")


async def test_read_missing_returns_empty():
    store = _store(_FakeEngine(row=None))
    assert await store.read("T1") == ()
