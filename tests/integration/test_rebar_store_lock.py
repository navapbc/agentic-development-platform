"""Two processes writing to one rebar ticket store serialize their writes through rebar's lock."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from scripts import rebar_lock_check

_CHECK = [sys.executable, "-m", "scripts.rebar_lock_check"]


def _writer(root: Path, name: str, count: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [*_CHECK, "write", "--root", str(root), "--writer", name, "--count", str(count)],
        stdout=subprocess.PIPE,
        text=True,
    )


def _written(process: subprocess.Popen[str]) -> int:
    out, _ = process.communicate(timeout=180)
    assert process.returncode == 0
    return json.loads(out.strip().splitlines()[-1])["written"]


def test_two_concurrent_writers_land_every_write(tmp_path: Path) -> None:
    store = tmp_path / "store"
    rebar_lock_check.init_store(store)

    writers = [_writer(store, name, 12) for name in ("a", "b")]
    counts = {name: _written(process) for name, process in zip(("a", "b"), writers, strict=True)}

    assert counts == {"a": 12, "b": 12}
    assert rebar_lock_check.verify(store, counts) == []


def test_a_writer_stopped_with_sigterm_leaves_no_lock(tmp_path: Path) -> None:
    store = tmp_path / "store"
    rebar_lock_check.init_store(store)
    stopped, steady = _writer(store, "stopped", 500), _writer(store, "steady", 8)

    time.sleep(3)
    stopped.send_signal(signal.SIGTERM)
    counts = {"stopped": _written(stopped), "steady": _written(steady)}

    assert 0 < counts["stopped"] < 500
    assert rebar_lock_check.verify(store, counts) == []


def test_a_lost_write_and_a_stale_lock_are_reported(tmp_path: Path) -> None:
    store = tmp_path / "store"
    rebar_lock_check.init_store(store)
    rebar_lock_check.write(store, "a", 2)
    (store / rebar_lock_check.TRACKER / ".ticket-write.lock.d").mkdir()

    problems = rebar_lock_check.verify(store, {"a": 3})

    assert len(problems) == 2
    assert "writer a wrote 3 tickets and the store holds [0, 1]" in problems[0]
    assert problems[1].endswith(".ticket-write.lock.d was left behind")
