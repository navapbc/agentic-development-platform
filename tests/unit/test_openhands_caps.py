"""Runtime caps: tool-call counting and wall-clock enforcement."""

from __future__ import annotations

import pytest
from openhands.runtime.caps import (
    TOOL_CALLS_EXCEEDED,
    WALL_CLOCK_EXCEEDED,
    CapTracker,
    RuntimeCapExceeded,
    RuntimeCaps,
)


def test_tool_call_within_cap_does_not_raise() -> None:
    tracker = CapTracker(RuntimeCaps(max_tool_calls=2))
    tracker.record_tool_call()
    tracker.record_tool_call()
    assert tracker.tool_calls == 2


def test_tool_call_beyond_cap_raises() -> None:
    tracker = CapTracker(RuntimeCaps(max_tool_calls=1))
    tracker.record_tool_call()
    with pytest.raises(RuntimeCapExceeded) as excinfo:
        tracker.record_tool_call()
    assert excinfo.value.reason == TOOL_CALLS_EXCEEDED
    assert excinfo.value.tool_calls == 2


def test_wall_clock_beyond_cap_raises_with_injected_clock() -> None:
    ticks = iter([0.0, 5.0])
    tracker = CapTracker(RuntimeCaps(max_wall_clock_seconds=1.0), clock=lambda: next(ticks))
    with pytest.raises(RuntimeCapExceeded) as excinfo:
        tracker.check_wall_clock()
    assert excinfo.value.reason == WALL_CLOCK_EXCEEDED


def test_wall_clock_within_cap_does_not_raise() -> None:
    ticks = iter([0.0, 0.5])
    tracker = CapTracker(RuntimeCaps(max_wall_clock_seconds=1.0), clock=lambda: next(ticks))
    tracker.check_wall_clock()
    assert tracker.tool_calls == 0
