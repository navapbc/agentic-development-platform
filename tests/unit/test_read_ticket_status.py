from __future__ import annotations

import pytest
from services.integration.jira.credentials import StatusFlow
from services.orchestration.temporal_activities.pull_request import (
    ReadStatusRequest,
    read_ticket_status,
)


class _Backend:
    def __init__(self, status: str) -> None:
        self._status = status

    def get_status(self, issue_key: str) -> str:
        return self._status


@pytest.fixture
def _patch(monkeypatch):
    def apply(status: str, terminal: str) -> None:
        monkeypatch.setattr(
            "services.integration.tickets.build_ticket_backend_for_issue",
            lambda issue_key, settings=None, resolver=None: _Backend(status),
        )
        monkeypatch.setattr(
            "services.integration.tickets.resolve_status_flow",
            lambda settings=None, *, project, resolver=None: StatusFlow(terminal=terminal),
        )

    return apply


@pytest.mark.asyncio
async def test_reads_terminal_status(_patch) -> None:
    _patch("closed", "closed")
    result = await read_ticket_status(ReadStatusRequest(issue_key="ADSER-1"))
    assert result.status == "closed"
    assert result.is_terminal is True


@pytest.mark.asyncio
async def test_non_terminal_status_is_not_terminal(_patch) -> None:
    _patch("in_progress", "closed")
    result = await read_ticket_status(ReadStatusRequest(issue_key="ADSER-1"))
    assert result.is_terminal is False


@pytest.mark.asyncio
async def test_unset_terminal_flow_is_never_terminal(_patch) -> None:
    _patch("closed", "")
    result = await read_ticket_status(ReadStatusRequest(issue_key="ADSER-1"))
    assert result.is_terminal is False


@pytest.mark.asyncio
async def test_no_issue_key_is_not_terminal() -> None:
    result = await read_ticket_status(ReadStatusRequest(issue_key=""))
    assert result.status == ""
    assert result.is_terminal is False
