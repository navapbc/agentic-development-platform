"""Unit tests for the intake source flag and the per-source intake schedule."""

from __future__ import annotations

import pytest
import scripts.intake as intake
from scripts.intake import (
    IntakeSourceMismatch,
    install_schedule,
    intake_schedule_id,
    resolve_intake_source,
)
from temporalio.service import RPCError, RPCStatusCode
from tests.support import hermetic_settings


class _Handle:
    def __init__(self, client: _Client, schedule_id: str) -> None:
        self._client = client
        self._id = schedule_id

    async def delete(self) -> None:
        if self._id in self._client.failing:
            raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        if self._id not in self._client.schedules:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        self._client.schedules.discard(self._id)
        self._client.deleted.append(self._id)


class _Client:
    def __init__(self, *existing: str, failing: tuple[str, ...] = ()) -> None:
        self.schedules = set(existing)
        self.failing = set(failing)
        self.created: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def get_schedule_handle(self, schedule_id: str) -> _Handle:
        return _Handle(self, schedule_id)

    async def create_schedule(self, schedule_id: str, schedule) -> None:
        if schedule_id in self.schedules:
            raise RuntimeError("schedule already exists")
        self.schedules.add(schedule_id)
        self.created.append((schedule_id, schedule.action.id))


@pytest.fixture
def rebar_settings(monkeypatch):
    settings = hermetic_settings(ticket_backend="rebar")
    monkeypatch.setattr(intake, "get_settings", lambda: settings)
    return settings


def test_schedule_id_names_the_source():
    settings = hermetic_settings()
    assert intake_schedule_id(settings, "rebar") == "rebar-intake"
    assert intake_schedule_id(settings, "jira") == "jira-intake"


def test_schedule_id_honors_an_explicit_override():
    settings = hermetic_settings(intake_schedule_id="custom-intake")
    assert intake_schedule_id(settings, "rebar") == "custom-intake"


def test_source_defaults_to_the_ticket_backend():
    assert resolve_intake_source(None, hermetic_settings(ticket_backend="rebar")) == "rebar"
    assert resolve_intake_source(None, hermetic_settings()) == "jira"


def test_source_matching_the_ticket_backend_is_accepted():
    assert resolve_intake_source("REBAR", hermetic_settings(ticket_backend="rebar")) == "rebar"


def test_source_other_than_the_ticket_backend_is_refused():
    with pytest.raises(IntakeSourceMismatch) as excinfo:
        resolve_intake_source("jira", hermetic_settings(ticket_backend="rebar"))
    assert "TICKET_BACKEND is rebar" in str(excinfo.value)


async def test_install_creates_the_source_schedule(rebar_settings):
    client = _Client()
    assert await install_schedule("rebar", client=client) == 0
    assert client.created == [("rebar-intake", "rebar-intake-run")]
    assert client.schedules == {"rebar-intake"}


async def test_install_replaces_an_existing_source_schedule(rebar_settings):
    client = _Client("rebar-intake")
    await install_schedule("rebar", client=client)
    assert client.deleted == ["rebar-intake"]
    assert client.schedules == {"rebar-intake"}


async def test_install_removes_the_inactive_source_schedule(rebar_settings, capsys):
    client = _Client("jira-intake")
    await install_schedule("rebar", client=client)
    assert client.schedules == {"rebar-intake"}
    assert "removed schedule 'jira-intake' for the inactive jira source" in capsys.readouterr().out


async def test_install_propagates_an_error_other_than_not_found(rebar_settings):
    client = _Client("jira-intake", failing=("jira-intake",))
    with pytest.raises(RPCError):
        await install_schedule("rebar", client=client)
