"""A dated resolution pin must not outlive the rolling window it was added to bypass."""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _uv_config() -> dict[str, object]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["uv"]


def _window() -> dt.timedelta:
    declared = str(_uv_config()["exclude-newer"])
    match = re.fullmatch(r"(\d+)\s*days?", declared.strip())
    assert match, f"exclude-newer is no longer a day count: {declared}"
    return dt.timedelta(days=int(match.group(1)))


def _dated_pins() -> dict[str, dt.datetime]:
    declared = _uv_config().get("exclude-newer-package") or {}
    assert isinstance(declared, dict)
    return {
        package: dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        for package, value in declared.items()
        if value is not False
    }


def test_the_rolling_window_is_a_day_count():
    assert _window() > dt.timedelta(0)


@pytest.mark.parametrize("package", sorted(_dated_pins()))
def test_a_dated_pin_is_still_inside_the_rolling_window(package: str):
    """A pin the window has caught up with no longer admits a release, it freezes one out."""
    pinned = _dated_pins()[package]
    caught_up_on = pinned + _window()
    now = dt.datetime.now(dt.UTC)

    assert now < caught_up_on, (
        f"{package} is pinned to {pinned.date().isoformat()}, which the "
        f"{_window().days}-day window passed on {caught_up_on.date().isoformat()}. "
        f"The pin now blocks every later release. Remove it from exclude-newer-package "
        f"in pyproject.toml and run uv lock --upgrade-package {package}."
    )
