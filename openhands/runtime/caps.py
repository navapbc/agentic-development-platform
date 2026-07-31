"""Runtime caps enforcement for the OpenHands sandbox implementation stage."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

COMPLETED = "completed"
TOOL_CALLS_EXCEEDED = "tool_calls_exceeded"
WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
ERROR = "error"


@dataclass(frozen=True)
class RuntimeCaps:
    max_tool_calls: int = 60
    max_wall_clock_seconds: float = 1800.0


class RuntimeCapExceeded(Exception):
    def __init__(self, reason: str, *, tool_calls: int, elapsed_seconds: float) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool_calls = tool_calls
        self.elapsed_seconds = elapsed_seconds


class CapTracker:
    def __init__(self, caps: RuntimeCaps, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._caps = caps
        self._clock = clock
        self._start = clock()
        self.tool_calls = 0

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    def record_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self._caps.max_tool_calls:
            raise RuntimeCapExceeded(
                TOOL_CALLS_EXCEEDED,
                tool_calls=self.tool_calls,
                elapsed_seconds=self.elapsed,
            )

    def exceeded_wall_clock(self) -> bool:
        return self.elapsed > self._caps.max_wall_clock_seconds

    def check_wall_clock(self) -> None:
        elapsed = self.elapsed
        if elapsed > self._caps.max_wall_clock_seconds:
            raise RuntimeCapExceeded(
                WALL_CLOCK_EXCEEDED,
                tool_calls=self.tool_calls,
                elapsed_seconds=elapsed,
            )
