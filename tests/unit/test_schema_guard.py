"""Unit tests for the startup check that holds services until the schema is at head."""

from __future__ import annotations

import logging

import pytest
from database.schema_guard import (
    SchemaAheadOfImage,
    SchemaState,
    classify,
    migration_script,
    wait_for_schema_head,
)

SCRIPT = migration_script()
HEAD = SCRIPT.get_heads()[0]
_HEAD_SCRIPT = SCRIPT.get_revision(HEAD)
assert _HEAD_SCRIPT is not None and isinstance(_HEAD_SCRIPT.down_revision, str)
PREVIOUS: str = _HEAD_SCRIPT.down_revision


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _fetcher(*results):
    pending = list(results)

    async def fetch() -> set[str]:
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    return fetch


def test_classify_reports_current_at_head():
    assert classify({HEAD}, SCRIPT) is SchemaState.CURRENT


def test_classify_reports_behind_for_an_earlier_revision():
    assert classify({PREVIOUS}, SCRIPT) is SchemaState.BEHIND


def test_classify_reports_behind_for_an_unmigrated_database():
    assert classify(set(), SCRIPT) is SchemaState.BEHIND


def test_classify_reports_unknown_for_a_revision_the_image_lacks():
    assert classify({"ffffffffffff"}, SCRIPT) is SchemaState.UNKNOWN


async def test_wait_returns_without_sleeping_when_current():
    clock = _Clock()
    await wait_for_schema_head(_fetcher({HEAD}), script=SCRIPT, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == []


async def test_wait_polls_until_the_schema_reaches_head(caplog):
    clock = _Clock()
    fetch = _fetcher(set(), {PREVIOUS}, {PREVIOUS}, {HEAD})
    with caplog.at_level(logging.WARNING, logger="schema-guard"):
        await wait_for_schema_head(
            fetch,
            script=SCRIPT,
            poll_seconds=5,
            log_every_seconds=30,
            sleep=clock.sleep,
            clock=clock,
        )
    assert clock.sleeps == [5, 5, 5]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Run inv db-migrate" in warnings[0]


async def test_wait_logs_again_after_the_log_interval(caplog):
    clock = _Clock()
    fetch = _fetcher(*([set()] * 8), {HEAD})
    with caplog.at_level(logging.WARNING, logger="schema-guard"):
        await wait_for_schema_head(
            fetch,
            script=SCRIPT,
            poll_seconds=5,
            log_every_seconds=30,
            sleep=clock.sleep,
            clock=clock,
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


async def test_wait_retries_when_the_revision_cannot_be_read(caplog):
    clock = _Clock()
    fetch = _fetcher(ConnectionRefusedError("postgres starting"), {HEAD})
    with caplog.at_level(logging.WARNING, logger="schema-guard"):
        await wait_for_schema_head(fetch, script=SCRIPT, sleep=clock.sleep, clock=clock)
    assert len(clock.sleeps) == 1
    assert "could not read the schema revision" in caplog.records[0].getMessage()


async def test_wait_exits_when_the_database_is_ahead_of_the_image():
    clock = _Clock()
    with pytest.raises(SchemaAheadOfImage) as excinfo:
        await wait_for_schema_head(
            _fetcher({"ffffffffffff"}), script=SCRIPT, sleep=clock.sleep, clock=clock
        )
    assert "ffffffffffff" in str(excinfo.value)
    assert clock.sleeps == []
