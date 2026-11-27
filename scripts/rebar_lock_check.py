"""Check that concurrent writers serialize their writes to one rebar ticket store.

A tenant's rebar store lives on a volume that more than one worker pod can mount. Run `init` once
on a scratch directory of that volume, run one `write` per writer at the same time, from separate
processes or from pods on separate nodes, then run `verify`. Never point the check at a tenant's
ticket store, because the writers add tickets to the store they are given.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from pathlib import Path

from services import process

TRACKER = ".tickets-tracker"
_TITLE = re.compile(r"^lock-check (?P<writer>\S+) (?P<index>\d+)$")


def init_store(root: Path) -> None:
    import rebar

    def git(*args: str) -> None:
        process.run(
            ["git", *args], cwd=root, check=True, timeout=process.SHORT_COMMAND_TIMEOUT_SECONDS
        )

    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git("init", "-q", "-b", "main")
        for key, value in (
            ("user.email", "lock-check@users.noreply.invalid"),
            ("user.name", "lock-check"),
        ):
            git("config", key, value)
    rebar.init_repo(repo_root=str(root), force_new_store=True)
    if not rebar.resolve_current_identity(repo_root=str(root)):
        identity = rebar.create_identity(
            "lock-check", "lock-check@users.noreply.invalid", repo_root=str(root)
        )
        rebar.use_identity(str(identity), repo_root=str(root))


def write(root: Path, writer: str, count: int) -> int:
    """Create up to `count` tickets as `writer`, stopping after the current write on SIGTERM."""
    import rebar

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    written = 0
    while written < count and not stopping:
        rebar.create_ticket("task", f"lock-check {writer} {written}", repo_root=str(root))
        written += 1
    return written


def verify(root: Path, expected: dict[str, int]) -> list[str]:
    """Return every problem with the store: a lost or duplicated write, or a lock left behind."""
    import rebar
    from rebar._store.lock import WRITE_LOCK_NAME

    seen: dict[str, list[int]] = {}
    for ticket in rebar.list_tickets(repo_root=str(root)):
        match = _TITLE.match(str(ticket.get("title") or ""))
        if match:
            seen.setdefault(match["writer"], []).append(int(match["index"]))
    problems = []
    for writer, count in expected.items():
        indexes = sorted(seen.get(writer, []))
        if indexes != list(range(count)):
            problems.append(f"writer {writer} wrote {count} tickets and the store holds {indexes}")
    lock_dir = root / TRACKER / f"{WRITE_LOCK_NAME}.d"
    if lock_dir.exists():
        problems.append(f"{lock_dir} was left behind")
    return problems


def _expected(values: list[str]) -> dict[str, int]:
    pairs = (value.partition("=") for value in values)
    return {writer: int(count) for writer, _, count in pairs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("init", "write", "verify"):
        sub.add_parser(mode).add_argument("--root", required=True, type=Path)
    sub.choices["write"].add_argument("--writer", required=True)
    sub.choices["write"].add_argument("--count", required=True, type=int)
    sub.choices["verify"].add_argument(
        "--expect", action="append", default=[], help="A writer and its count, as NAME=COUNT."
    )
    args = parser.parse_args(argv)
    if args.mode == "init":
        init_store(args.root)
        return 0
    if args.mode == "write":
        written = write(args.root, args.writer, args.count)
        print(json.dumps({"writer": args.writer, "written": written}))
        return 0
    problems = verify(args.root, _expected(args.expect))
    for problem in problems:
        print(f"[rebar-lock-check] {problem}")
    print("[rebar-lock-check] " + ("FAIL" if problems else "PASS"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
