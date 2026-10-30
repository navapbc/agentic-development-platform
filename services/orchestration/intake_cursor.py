"""Release the backend cursor that decides whether intake offers a ticket again.

Clearing the side-effect ledger is enough for a level-triggered backend, which enumerates every
open ticket on each pass. The rebar poll is edge-triggered against a file watermark, so a ticket
whose revision is already recorded is never offered again until that entry is released too.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from services.integration.tickets import REBAR, resolve_ticket_backend_name


@dataclass(frozen=True)
class CursorRelease:
    released: int = 0
    warning: str = ""


class WatermarkRelease(Protocol):
    def exists(self) -> bool: ...

    def release(self, ticket_id: str) -> int: ...


class IntakeCursorRelease(Protocol):
    async def release(self, ticket_key: str) -> CursorRelease: ...


class LedgerOnlyIntakeCursor:
    """Jira lists every open issue per pass, so the ledger is its only intake cursor."""

    async def release(self, ticket_key: str) -> CursorRelease:
        return CursorRelease()


class RebarWatermarkIntakeCursor:
    def __init__(
        self,
        watermark: WatermarkRelease,
        *,
        path: Path,
        readiness: Callable[[str], bool],
    ) -> None:
        self._watermark = watermark
        self._path = path
        self._readiness = readiness

    async def release(self, ticket_key: str) -> CursorRelease:
        if not self._watermark.exists():
            return CursorRelease(
                0, f"no rebar poll cursor exists at {self._path}, so none was released"
            )
        if not self._watermark.release(ticket_key):
            return CursorRelease(
                0,
                f"the rebar poll cursor at {self._path} does not hold {ticket_key}, so the next "
                "pass will offer it regardless",
            )
        return CursorRelease(1, self._readiness_warning(ticket_key))

    def _readiness_warning(self, ticket_key: str) -> str:
        try:
            ready = self._readiness(ticket_key)
        except Exception as error:  # noqa: BLE001  readiness is advisory, so it may not block
            return f"could not read the rebar store to check whether {ticket_key} is ready, {error}"
        if ready:
            return ""
        return (
            f"the rebar store does not list {ticket_key} as ready, so the next poll will "
            "report it blocked rather than start it"
        )


def build_intake_cursor_release(settings: Any) -> IntakeCursorRelease:
    if resolve_ticket_backend_name(settings) != REBAR:
        return LedgerOnlyIntakeCursor()

    from scripts.rebar_poll import FileWatermarkStore, watermark_path

    from services.integration.tickets.rebar_config import load_rebar_backend_config

    repo_root = load_rebar_backend_config(settings).repo_root
    path = watermark_path(repo_root)
    return RebarWatermarkIntakeCursor(
        FileWatermarkStore(path), path=path, readiness=_readiness_probe(repo_root)
    )


def _readiness_probe(repo_root: str) -> Callable[[str], bool]:
    def _probe(ticket_key: str) -> bool:
        import rebar

        return any(
            str(ticket.get("ticket_id") or "") == ticket_key
            for ticket in rebar.ready(repo_root=repo_root)
        )

    return _probe
