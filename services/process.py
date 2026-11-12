"""The single place the platform starts an external process.

Every command runs from an argument list with no shell, and its executable is resolved to an
absolute path before it starts, so a relative name cannot be satisfied by a file planted in the
working directory or by a later PATH change. `run` and `run_async` take a keyword-only `timeout`
with no default, so every bounded call states its limit. `popen` is for processes whose lifetime
the caller manages, such as pipelines and long-lived servers. `replace_process` hands the current
process over to a command, as an interactive tunnel does.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

CalledProcessError = subprocess.CalledProcessError
CompletedProcess = subprocess.CompletedProcess
DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE
Popen = subprocess.Popen
SubprocessError = subprocess.SubprocessError
TimeoutExpired = subprocess.TimeoutExpired

Argv = Sequence[str | os.PathLike[str]]

SHORT_COMMAND_TIMEOUT_SECONDS = 60.0
MEDIUM_COMMAND_TIMEOUT_SECONDS = 900.0
LONG_COMMAND_TIMEOUT_SECONDS = 3600.0

_FORBIDDEN_OPTIONS = frozenset({"shell", "executable"})


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def resolve_executable(name: str, *, path: str | None = None) -> str:
    """Return the absolute path of an executable, raising FileNotFoundError when there is none.

    A name containing a path separator is taken as a path and must name an executable file. A
    bare name is looked up on `path`, or on PATH when `path` is None.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        candidate = os.path.abspath(name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        raise FileNotFoundError(errno.ENOENT, f"not an executable file: {name}", name)
    found = shutil.which(name, path=path)
    if found is None:
        raise FileNotFoundError(errno.ENOENT, f"executable not found on PATH: {name}", name)
    return os.path.abspath(found)


def _argv(args: Argv) -> list[str]:
    if isinstance(args, str | bytes):
        raise TypeError("pass the command as an argument list, not a string")
    argv = [os.fspath(arg) for arg in args]
    if not argv:
        raise ValueError("the argument list is empty")
    return argv


def _search_path(env: Mapping[str, str] | None) -> str | None:
    return (env if env is not None else os.environ).get("PATH")


def _checked_options(options: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = _FORBIDDEN_OPTIONS.intersection(options)
    if forbidden:
        raise TypeError(f"unsupported process options: {sorted(forbidden)}")
    return dict(options)


def run(args: Argv, *, timeout: float | None, **options: Any) -> CompletedProcess[Any]:
    """Run a command to completion with the `subprocess.run` options the caller passes."""
    argv = _argv(args)
    checked = _checked_options(options)
    executable = resolve_executable(argv[0], path=_search_path(checked.get("env")))
    return subprocess.run(argv, executable=executable, shell=False, timeout=timeout, **checked)


def popen(args: Argv, **options: Any) -> Popen[Any]:
    """Start a command whose lifetime the caller manages, with `subprocess.Popen` options."""
    argv = _argv(args)
    checked = _checked_options(options)
    executable = resolve_executable(argv[0], path=_search_path(checked.get("env")))
    return subprocess.Popen(argv, executable=executable, shell=False, **checked)


async def run_async(
    args: Argv,
    *,
    timeout: float | None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> ProcessResult:
    """Run a command on the event loop, capturing its output and killing it at the timeout."""
    argv = _argv(args)
    executable = resolve_executable(argv[0], path=_search_path(env))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        executable=executable,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=PIPE if input is not None else None,
        stdout=PIPE,
        stderr=PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutExpired(argv, timeout or 0.0) from None
    return ProcessResult(returncode=proc.returncode or 0, stdout=stdout, stderr=stderr)


def replace_process(args: Argv, env: Mapping[str, str]) -> NoReturn:
    """Replace the current process with the command, which inherits the process ID."""
    argv = _argv(args)
    os.execve(resolve_executable(argv[0], path=env.get("PATH")), argv, dict(env))
