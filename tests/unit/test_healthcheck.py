"""Tests for the container health probes and the worker heartbeat."""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from services import healthcheck
from services.repository_intelligence.indexing.worker import RepoIndexWorker


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/health" else 503)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def server_port():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_http_probe_passes_on_a_healthy_endpoint(server_port: int):
    assert healthcheck.main(["http", str(server_port)]) == 0


def test_http_probe_fails_on_an_error_status(server_port: int):
    assert healthcheck.main(["http", str(server_port), "/down"]) == 1


def test_http_probe_fails_when_nothing_listens(server_port: int):
    assert healthcheck.http_ok(server_port, "/health") is True
    assert healthcheck.http_ok(1, "/health", timeout=0.5) is False


def test_heartbeat_probe_reads_the_file_age(tmp_path: Path):
    beat = tmp_path / "worker.heartbeat"
    assert healthcheck.main(["heartbeat", str(beat), "--max-age", "60"]) == 1

    healthcheck.touch(beat)
    mtime = beat.stat().st_mtime

    assert healthcheck.heartbeat_fresh(beat, 60, now=lambda: mtime + 59)
    assert not healthcheck.heartbeat_fresh(beat, 60, now=lambda: mtime + 61)
    assert healthcheck.main(["heartbeat", str(beat), "--max-age", "60"]) == 0


async def test_heartbeat_refreshes_only_while_the_worker_runs(tmp_path: Path):
    beat = tmp_path / "beat"
    alive = {"value": False}
    task = asyncio.create_task(
        healthcheck.heartbeat(beat, interval=0.01, alive=lambda: alive["value"])
    )
    await asyncio.sleep(0.05)
    assert not beat.exists()

    alive["value"] = True
    await asyncio.sleep(0.05)
    task.cancel()

    assert beat.exists()


def test_repo_index_worker_touches_its_heartbeat_each_poll(tmp_path: Path):
    beat = tmp_path / "beat"
    stop = threading.Event()
    worker = RepoIndexWorker([], commit_resolver=lambda root: "")
    loop = threading.Thread(
        target=worker.run_forever,
        kwargs={"interval": 0.01, "stop": stop, "heartbeat_file": beat},
    )
    loop.start()
    try:
        for _ in range(200):
            if beat.exists():
                break
            stop.wait(0.01)
    finally:
        stop.set()
        loop.join(timeout=5)
        worker.close()

    assert beat.exists()
