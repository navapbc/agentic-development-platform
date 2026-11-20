"""Tests for the process execution boundary."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from services import process


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_resolve_executable_returns_an_absolute_path_from_the_search_path(tmp_path):
    tool = _script(tmp_path, "platform-tool", "exit 0")

    assert process.resolve_executable("platform-tool", path=str(tmp_path)) == str(tool)


def test_resolve_executable_rejects_a_missing_command(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        process.resolve_executable("platform-tool", path=str(tmp_path))


def test_resolve_executable_rejects_a_path_that_is_not_executable(tmp_path):
    plain = tmp_path / "plain"
    plain.write_text("data", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="not an executable file"):
        process.resolve_executable(str(plain))


def test_resolve_executable_ignores_a_same_named_file_in_the_working_directory(
    tmp_path, monkeypatch
):
    planted = tmp_path / "cwd"
    planted.mkdir()
    _script(planted, "platform-tool", "exit 7")
    trusted = tmp_path / "bin"
    trusted.mkdir()
    tool = _script(trusted, "platform-tool", "exit 0")
    monkeypatch.chdir(planted)

    assert process.resolve_executable("platform-tool", path=str(trusted)) == str(tool)


def test_run_keeps_the_command_name_as_argv_zero(tmp_path):
    _script(tmp_path, "platform-tool", 'printf "%s" "$0"')
    env = {**os.environ, "PATH": str(tmp_path)}

    result = process.run(["platform-tool"], timeout=10, env=env, capture_output=True, text=True)

    assert result.args == ["platform-tool"]
    assert result.stdout == str(tmp_path / "platform-tool")


def test_run_refuses_a_command_string():
    with pytest.raises(TypeError, match="argument list"):
        process.run("git status", timeout=10)


def test_run_refuses_an_empty_argument_list():
    with pytest.raises(ValueError, match="empty"):
        process.run([], timeout=10)


@pytest.mark.parametrize("option", ["shell", "executable"])
def test_run_refuses_options_that_bypass_resolution(option):
    with pytest.raises(TypeError, match="unsupported process options"):
        process.run([sys.executable, "-c", "pass"], timeout=10, **{option: True})


def test_run_enforces_the_timeout():
    with pytest.raises(process.TimeoutExpired):
        process.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


def test_popen_resolves_the_executable(tmp_path):
    _script(tmp_path, "platform-tool", "exit 3")

    proc = process.popen(["platform-tool"], env={"PATH": str(tmp_path)})

    assert proc.wait(timeout=10) == 3


async def test_run_async_captures_output_and_feeds_input():
    result = await process.run_async(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        timeout=10,
        input=b"payload",
    )

    assert result == process.ProcessResult(returncode=0, stdout=b"PAYLOAD", stderr=b"")


async def test_run_async_kills_the_process_at_the_timeout():
    with pytest.raises(process.TimeoutExpired):
        await process.run_async([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


async def test_run_async_reports_a_missing_command(tmp_path):
    with pytest.raises(FileNotFoundError):
        await process.run_async(["platform-tool"], timeout=10, env={"PATH": str(tmp_path)})
