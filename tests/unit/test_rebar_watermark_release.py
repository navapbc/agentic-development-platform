"""Releasing a rebar ticket from the poll watermark so intake offers it again."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.rebar_poll import (
    NEW,
    RELEASES_DIRNAME,
    AccessDecision,
    FileWatermarkStore,
    PollCursor,
    run_poll,
    watermark_path,
)

_TICKET = {"ticket_id": "A", "status": "open", "title": "T"}


def _markers(root: Path) -> list[Path]:
    return sorted((root / RELEASES_DIRNAME).glob("*.release"))


class _Reader:
    def branch_head(self) -> str:
        return "commit-1"

    def list_reduced(self) -> list[dict]:
        return [dict(_TICKET)]


class _Access:
    def authorize(self) -> AccessDecision:
        return AccessDecision(authorized=True, identity="reader@x", role="tickets-reader")


class _Audit:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, event: dict) -> None:
        self.records.append(event)

    def events(self) -> list[str]:
        return [record["event"] for record in self.records]


def _seeded(path: Path) -> FileWatermarkStore:
    FileWatermarkStore(path).save(
        PollCursor(
            branch_commit="commit-1",
            revisions={"A": "rev-a", "B": "rev-b"},
            statuses={"A": "open", "B": "closed"},
        )
    )
    return FileWatermarkStore(path)


def test_a_released_ticket_is_gone_from_the_next_cursor_the_poll_loads(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    store = _seeded(path)

    assert store.release("A") == 1

    cursor = FileWatermarkStore(path).load()
    assert "A" not in cursor.revisions
    assert "A" not in cursor.statuses
    assert cursor.revisions["B"] == "rev-b"
    assert cursor.branch_commit == "commit-1"
    assert not list(tmp_path.glob("*.tmp"))


def test_release_reports_nothing_when_the_cursor_never_held_the_ticket(tmp_path: Path) -> None:
    store = _seeded(watermark_path(str(tmp_path)))

    assert store.release("absent") == 0


def test_release_against_a_store_that_does_not_exist_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    store = FileWatermarkStore(watermark_path(str(root)))

    assert store.release("A") == 0
    assert not root.exists()


def test_a_poll_pass_in_flight_does_not_resurrect_a_released_ticket(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    pass_store = FileWatermarkStore(path)
    in_flight = pass_store.load()

    FileWatermarkStore(path).release("A")
    pass_store.save(in_flight)

    assert "A" not in FileWatermarkStore(path).load().revisions


def test_a_release_survives_a_pass_that_advanced_that_same_ticket(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    pass_store = FileWatermarkStore(path)
    in_flight = pass_store.load()
    in_flight.revisions["A"] = "rev-a2"
    in_flight.statuses["A"] = "in_progress"

    FileWatermarkStore(path).release("A")
    pass_store.save(in_flight)

    assert "A" not in FileWatermarkStore(path).load().revisions


def test_a_pass_that_consumed_a_release_does_not_replay_it_forever(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    FileWatermarkStore(path).release("A")

    pass_store = FileWatermarkStore(path)
    cursor = pass_store.load()
    cursor.revisions["A"] = "rev-a"
    cursor.statuses["A"] = "open"
    pass_store.save(cursor)

    assert FileWatermarkStore(path).load().revisions["A"] == "rev-a"


def test_a_pass_that_crashes_before_saving_leaves_the_release_pending(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    FileWatermarkStore(path).release("A")

    FileWatermarkStore(path).load()

    assert _markers(tmp_path)
    assert "A" not in FileWatermarkStore(path).load().revisions


def test_a_second_release_during_a_pass_is_not_deleted_by_that_pass(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    FileWatermarkStore(path).release("A")

    pass_store = FileWatermarkStore(path)
    cursor = pass_store.load()
    cursor.revisions["A"] = "rev-a"
    cursor.statuses["A"] = "open"
    # The operator sees nothing happen yet and runs the release again before the pass saves.
    assert FileWatermarkStore(path).release("A") == 1
    pass_store.save(cursor)

    assert _markers(tmp_path)
    assert "A" not in FileWatermarkStore(path).load().revisions


def test_two_passes_honouring_one_release_do_not_delete_a_later_one(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    FileWatermarkStore(path).release("A")

    first = FileWatermarkStore(path)
    second = FileWatermarkStore(path)
    first_cursor = first.load()
    second_cursor = second.load()
    first_cursor.revisions["A"] = "rev-a"
    first.save(first_cursor)
    assert FileWatermarkStore(path).release("A") == 1
    second_cursor.revisions["A"] = "rev-a"
    second.save(second_cursor)

    assert "A" not in FileWatermarkStore(path).load().revisions


def test_a_file_the_poll_did_not_write_is_neither_honoured_nor_deleted(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    _seeded(path)
    releases = tmp_path / RELEASES_DIRNAME
    releases.mkdir()
    keep = releases / ".gitkeep"
    keep.write_text("", encoding="utf-8")
    nested = releases / "subdir"
    nested.mkdir()

    store = FileWatermarkStore(path)
    store.save(store.load())

    assert keep.exists()
    assert nested.is_dir()


def test_a_ticket_id_that_is_not_a_safe_filename_still_round_trips(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    FileWatermarkStore(path).save(PollCursor(revisions={"PROJ/1": "rev"}, statuses={"PROJ/1": "x"}))

    assert FileWatermarkStore(path).release("PROJ/1") == 1
    assert "PROJ/1" not in FileWatermarkStore(path).load().revisions


@pytest.mark.asyncio
async def test_the_next_poll_offers_a_released_ticket_as_new(tmp_path: Path) -> None:
    path = watermark_path(str(tmp_path))
    admitted: list[str] = []

    async def intake(event) -> bool:
        admitted.append(event.ticket_id)
        return True

    async def poll(audit: _Audit) -> None:
        await run_poll(
            reader=_Reader(),
            watermark=FileWatermarkStore(path),
            audit=audit,
            access=_Access(),
            allowed_fields=("ticket_id", "status", "title"),
            intake=intake,
        )

    await poll(_Audit())
    settled = _Audit()
    await poll(settled)
    assert "rebar_poll_noop" in settled.events()
    assert admitted == ["A"]

    assert FileWatermarkStore(path).release("A") == 1

    rerun = _Audit()
    await poll(rerun)
    assert f"rebar_poll_{NEW}" in rerun.events()
    assert admitted == ["A", "A"]

    settled_again = _Audit()
    await poll(settled_again)
    assert "rebar_poll_noop" in settled_again.events()
    assert admitted == ["A", "A"]


def test_a_marker_that_cannot_be_unlinked_is_reported_rather_than_silently_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileWatermarkStore(watermark_path(str(tmp_path)))
    store.save(PollCursor(revisions={"ADEV-1": "rev-1"}))
    store.release("ADEV-1")
    store.load()

    def _refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("owned by another uid")

    monkeypatch.setattr(Path, "unlink", _refuse)
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        FileWatermarkStore,
        "_log",
        staticmethod(
            lambda: type(
                "_Sink",
                (),
                {"warning": lambda _self, event, **fields: warnings.append((event, fields))},
            )()
        ),
    )

    store.save(PollCursor(revisions={}))

    assert [event for event, _ in warnings] == ["rebar_poll_release_marker_stuck"]
    assert warnings[0][1]["ticket_id"] == "ADEV-1"
