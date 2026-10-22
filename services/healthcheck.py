"""Container health probes and the heartbeat that long-running workers refresh.

`python -m services.healthcheck http PORT [PATH]` succeeds when a loopback request answers
below 400. `python -m services.healthcheck heartbeat FILE --max-age SECONDS` succeeds when a
worker refreshed FILE within the window. A worker that serves no endpoint runs `heartbeat` or
calls `touch` from its own loop, so the file goes stale when the loop stops.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import time
from collections.abc import Callable, Sequence
from pathlib import Path

LOOPBACK = "127.0.0.1"


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


async def heartbeat(path: Path, *, interval: float, alive: Callable[[], bool]) -> None:
    """Refresh `path` every `interval` seconds while `alive()` holds. Runs until cancelled."""
    while True:
        if alive():
            touch(path)
        await asyncio.sleep(interval)


def http_ok(port: int, path: str = "/health", *, timeout: float = 3.0) -> bool:
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=timeout)
    try:
        connection.request("GET", path)
        return connection.getresponse().status < 400
    except (http.client.HTTPException, OSError):
        return False
    finally:
        connection.close()


def heartbeat_fresh(path: Path, max_age: float, *, now: Callable[[], float] = time.time) -> bool:
    try:
        return now() - path.stat().st_mtime <= max_age
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Container health probe.")
    probes = parser.add_subparsers(dest="probe", required=True)
    http_probe = probes.add_parser("http", help="GET a loopback endpoint.")
    http_probe.add_argument("port", type=int)
    http_probe.add_argument("path", nargs="?", default="/health")
    file_probe = probes.add_parser("heartbeat", help="Check a heartbeat file's age.")
    file_probe.add_argument("file", type=Path)
    file_probe.add_argument("--max-age", type=float, required=True)
    args = parser.parse_args(argv)
    if args.probe == "http":
        healthy = http_ok(args.port, args.path)
    else:
        healthy = heartbeat_fresh(args.file, args.max_age)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
