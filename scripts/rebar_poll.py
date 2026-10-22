"""Flag-driven change-detection poll over the rebar tickets branch.

Reads one branch-head snapshot against a persisted watermark, reports new
tickets, field changes, and status changes, runs under an authenticated reader
identity restricted to approved roles, redacts non-allowlisted fields, audits
every invocation, applies idempotency before side effects, and advances the
watermark only after intake or terminal signaling succeeds.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from services import process
from services.integration.jira.snapshot import content_hash

NEW = "new"
FIELD_CHANGE = "field_change"
STATUS_CHANGE = "status_change"


@dataclass
class PollCursor:
    branch_commit: str = ""
    revisions: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PollEvent:
    kind: str
    ticket_id: str
    revision: str
    branch_commit: str
    ticket: dict[str, Any]
    previous_status: str = ""


@dataclass(frozen=True)
class PollReport:
    branch_commit: str
    authorized: bool
    audited: bool
    events: tuple[PollEvent, ...] = ()
    advanced: bool = False
    reason: str = ""


class TicketStoreReader(Protocol):
    def branch_head(self) -> str: ...

    def list_reduced(self) -> list[dict[str, Any]]: ...


class WatermarkStore(Protocol):
    def load(self) -> PollCursor: ...

    def save(self, cursor: PollCursor) -> None: ...


class AuditSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


class IdempotencyGuard(Protocol):
    def seen(self, key: str) -> bool: ...

    def mark(self, key: str) -> None: ...


@dataclass(frozen=True)
class AccessDecision:
    authorized: bool
    identity: str
    role: str


class AccessPolicy(Protocol):
    def authorize(self) -> AccessDecision: ...


IntakeHandler = Callable[[PollEvent], Awaitable[bool]]


@dataclass(frozen=True)
class PollFlags:
    report_new: bool = True
    report_field_changes: bool = True
    report_status_changes: bool = True

    def includes(self, kind: str) -> bool:
        if kind == NEW:
            return self.report_new
        if kind == STATUS_CHANGE:
            return self.report_status_changes
        return self.report_field_changes


def _revision(ticket: Mapping[str, Any]) -> str:
    return content_hash(dict(ticket))


def redact(ticket: Mapping[str, Any], allowed_fields: Sequence[str]) -> dict[str, Any]:
    if not allowed_fields:
        return dict(ticket)
    allow = set(allowed_fields)
    return {key: value for key, value in ticket.items() if key in allow}


def _classify(ticket_id: str, revision: str, status: str, cursor: PollCursor) -> str | None:
    if ticket_id not in cursor.revisions:
        return NEW
    if cursor.revisions[ticket_id] == revision:
        return None
    if cursor.statuses.get(ticket_id, "") != status:
        return STATUS_CHANGE
    return FIELD_CHANGE


async def run_poll(
    *,
    reader: TicketStoreReader,
    watermark: WatermarkStore,
    audit: AuditSink,
    access: AccessPolicy,
    allowed_fields: Sequence[str],
    flags: PollFlags | None = None,
    intake: IntakeHandler | None = None,
    idempotency: IdempotencyGuard | None = None,
) -> PollReport:
    flags = flags or PollFlags()
    decision = access.authorize()
    audit.record(
        {
            "event": "rebar_poll_invoked",
            "identity": decision.identity,
            "role": decision.role,
            "authorized": decision.authorized,
        }
    )
    if not decision.authorized:
        audit.record(
            {
                "event": "rebar_poll_denied",
                "identity": decision.identity,
                "role": decision.role,
            }
        )
        return PollReport(branch_commit="", authorized=False, audited=True, reason="unauthorized")

    branch_commit = reader.branch_head()
    cursor = watermark.load()
    tickets = reader.list_reduced()

    events: list[PollEvent] = []
    for ticket in tickets:
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            continue
        revision = _revision(ticket)
        status = str(ticket.get("status") or "")
        kind = _classify(ticket_id, revision, status, cursor)
        if kind is None or not flags.includes(kind):
            continue
        events.append(
            PollEvent(
                kind=kind,
                ticket_id=ticket_id,
                revision=revision,
                branch_commit=branch_commit,
                ticket=redact(ticket, allowed_fields),
                previous_status=cursor.statuses.get(ticket_id, ""),
            )
        )

    if not events:
        audit.record({"event": "rebar_poll_noop", "branch_commit": branch_commit})
        cursor.branch_commit = branch_commit
        watermark.save(cursor)
        return PollReport(
            branch_commit=branch_commit, authorized=True, audited=True, reason="no_changes"
        )

    all_ok = True
    for event in events:
        audit.record(
            {
                "event": f"rebar_poll_{event.kind}",
                "ticket_id": event.ticket_id,
                "revision": event.revision,
                "branch_commit": event.branch_commit,
                "previous_status": event.previous_status,
            }
        )
        key = f"{event.ticket_id}:{event.revision}"
        if idempotency is not None and idempotency.seen(key):
            _advance(cursor, event)
            continue
        if intake is not None:
            ok = await intake(event)
            if not ok:
                all_ok = False
                continue
        if idempotency is not None:
            idempotency.mark(key)
        _advance(cursor, event)

    if all_ok:
        cursor.branch_commit = branch_commit
    watermark.save(cursor)
    return PollReport(
        branch_commit=branch_commit,
        authorized=True,
        audited=True,
        events=tuple(events),
        advanced=all_ok,
    )


def _advance(cursor: PollCursor, event: PollEvent) -> None:
    cursor.revisions[event.ticket_id] = event.revision
    status = str(event.ticket.get("status") or event.previous_status)
    cursor.statuses[event.ticket_id] = status


class GitTicketStoreReader:
    def __init__(self, repo_root: str, *, branch: str = "tickets") -> None:
        self._repo_root = repo_root
        self._branch = branch

    def branch_head(self) -> str:
        proc = process.run(
            ["git", "rev-parse", self._branch],
            timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS,
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def list_reduced(self) -> list[dict[str, Any]]:
        import rebar

        return [dict(t) for t in rebar.list_tickets(repo_root=self._repo_root)]


class FileWatermarkStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> PollCursor:
        if not self._path.exists():
            return PollCursor()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return PollCursor(
            branch_commit=str(raw.get("branch_commit", "")),
            revisions=dict(raw.get("revisions", {})),
            statuses=dict(raw.get("statuses", {})),
        )

    def save(self, cursor: PollCursor) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "branch_commit": cursor.branch_commit,
                    "revisions": cursor.revisions,
                    "statuses": cursor.statuses,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


class InMemoryIdempotencyGuard:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str) -> None:
        self._seen.add(key)


class StructlogAuditSink:
    def __init__(self) -> None:
        import structlog

        self._log = structlog.get_logger("rebar_poll")

    def record(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        name = payload.pop("event", "rebar_poll")
        self._log.info(name, **payload)


class RebarIdentityAccess:
    def __init__(self, repo_root: str, *, reader_roles: Sequence[str], identity_roles) -> None:
        self._repo_root = repo_root
        self._reader_roles = frozenset(reader_roles)
        self._identity_roles = identity_roles

    def authorize(self) -> AccessDecision:
        import rebar

        identity_id = rebar.resolve_current_identity(repo_root=self._repo_root) or ""
        email = rebar.identity_email(identity_id, repo_root=self._repo_root) if identity_id else ""
        email = email or ""
        role = self._identity_roles.role_for(email) if email else ""
        authorized = bool(email) and role in self._reader_roles
        return AccessDecision(authorized=authorized, identity=email, role=role)


class ReadOnlyWatermark:
    """Reads the intake cursor and discards saves, so a change report never moves intake."""

    def __init__(self, inner: WatermarkStore) -> None:
        self._inner = inner

    def load(self) -> PollCursor:
        return self._inner.load()

    def save(self, cursor: PollCursor) -> None:
        return None


def _build_flags(args: argparse.Namespace) -> PollFlags:
    selected = any((args.new, args.field_changes, args.status_changes))
    if not selected:
        return PollFlags()
    return PollFlags(
        report_new=args.new,
        report_field_changes=args.field_changes,
        report_status_changes=args.status_changes,
    )


def main(argv: list[str] | None = None) -> int:
    import asyncio

    from services.integration.tickets.rebar_config import (
        load_poll_config,
        load_rebar_backend_config,
    )

    parser = argparse.ArgumentParser(
        description=(
            "Report rebar ticket changes since the last intake pass. The report never starts "
            "work and never moves the intake cursor."
        )
    )
    parser.add_argument("--new", action="store_true", help="Report new tickets.")
    parser.add_argument("--field-changes", action="store_true", help="Report field changes.")
    parser.add_argument("--status-changes", action="store_true", help="Report status changes.")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    backend = load_rebar_backend_config()
    poll_config = load_poll_config()
    repo_root = backend.repo_root
    reader = GitTicketStoreReader(repo_root)
    watermark = ReadOnlyWatermark(
        FileWatermarkStore(Path(repo_root) / ".rebar-poll-watermark.json")
    )
    access = RebarIdentityAccess(
        repo_root,
        reader_roles=poll_config.reader_roles,
        identity_roles=poll_config,
    )
    report = asyncio.run(
        run_poll(
            reader=reader,
            watermark=watermark,
            audit=StructlogAuditSink(),
            access=access,
            allowed_fields=poll_config.allowed_fields,
            flags=_build_flags(args),
            idempotency=InMemoryIdempotencyGuard(),
        )
    )
    if not report.authorized:
        print("[rebar-changes] denied: unauthorized reader identity")
        return 1
    print(
        f"[rebar-changes] branch {report.branch_commit[:12]} "
        f"reported {len(report.events)} change(s)"
    )
    for event in report.events:
        print(f"[rebar-changes] {event.kind}: {event.ticket_id} rev {event.revision[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
