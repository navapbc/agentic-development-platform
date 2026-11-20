"""Choosing the intake cursor that a retry has to release for the configured backend."""

from __future__ import annotations

from pathlib import Path

import pytest
import rebar
from scripts.rebar_poll import FileWatermarkStore, PollCursor, watermark_path
from services.integration.tickets import rebar_config
from services.integration.tickets.rebar_config import RebarBackendConfig
from services.orchestration.intake_cursor import (
    LedgerOnlyIntakeCursor,
    build_intake_cursor_release,
)
from tests.support import hermetic_settings


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo_root: str,
    ready: tuple[str, ...] = (),
    ready_error: Exception | None = None,
) -> None:
    monkeypatch.setattr(
        rebar_config,
        "load_rebar_backend_config",
        lambda settings: RebarBackendConfig(repo_root=repo_root),
    )

    def _ready(**kwargs):
        if ready_error is not None:
            raise ready_error
        return [{"ticket_id": ticket} for ticket in ready]

    monkeypatch.setattr(rebar, "ready", _ready)


def _rebar_cursor():
    return build_intake_cursor_release(hermetic_settings(ticket_backend="rebar"))


@pytest.mark.asyncio
async def test_jira_has_no_cursor_to_release_beyond_the_ledger() -> None:
    release = build_intake_cursor_release(hermetic_settings(ticket_backend="jira"))

    assert isinstance(release, LedgerOnlyIntakeCursor)
    outcome = await release.release("ADEV-1")
    assert outcome.released == 0
    assert outcome.warning == ""


@pytest.mark.asyncio
async def test_rebar_releases_the_watermark_under_the_configured_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FileWatermarkStore(watermark_path(str(tmp_path))).save(
        PollCursor(revisions={"A": "rev-a"}, statuses={"A": "open"})
    )
    _configure(monkeypatch, repo_root=str(tmp_path), ready=("A",))

    outcome = await _rebar_cursor().release("A")

    assert outcome.released == 1
    assert outcome.warning == ""
    assert "A" not in FileWatermarkStore(watermark_path(str(tmp_path))).load().revisions


@pytest.mark.asyncio
async def test_a_ticket_the_store_does_not_call_ready_is_released_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FileWatermarkStore(watermark_path(str(tmp_path))).save(PollCursor(revisions={"A": "rev-a"}))
    _configure(monkeypatch, repo_root=str(tmp_path), ready=("B",))

    outcome = await _rebar_cursor().release("A")

    assert outcome.released == 1
    assert "does not list A as ready" in outcome.warning


@pytest.mark.asyncio
async def test_an_unreadable_rebar_store_warns_rather_than_failing_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FileWatermarkStore(watermark_path(str(tmp_path))).save(PollCursor(revisions={"A": "rev-a"}))
    _configure(monkeypatch, repo_root=str(tmp_path), ready_error=RuntimeError("no tracker"))

    outcome = await _rebar_cursor().release("A")

    assert outcome.released == 1
    assert "could not read the rebar store" in outcome.warning
    assert "no tracker" in outcome.warning


@pytest.mark.asyncio
async def test_a_cursor_that_no_longer_holds_the_ticket_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FileWatermarkStore(watermark_path(str(tmp_path))).save(PollCursor(revisions={"B": "rev-b"}))
    _configure(monkeypatch, repo_root=str(tmp_path), ready=("A",))

    outcome = await _rebar_cursor().release("A")

    assert outcome.released == 0
    assert "does not hold A" in outcome.warning


@pytest.mark.asyncio
async def test_a_cursor_file_that_does_not_exist_is_reported_rather_than_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, repo_root=str(tmp_path / "elsewhere"), ready=("A",))

    outcome = await _rebar_cursor().release("A")

    assert outcome.released == 0
    assert "no rebar poll cursor exists at" in outcome.warning
