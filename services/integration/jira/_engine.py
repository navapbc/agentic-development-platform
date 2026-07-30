"""Put rebar's reconciler engine directory on sys.path for in-process vendor-client use."""

from __future__ import annotations

import sys


def ensure_engine_on_path() -> None:
    from rebar._engine import engine_dir

    path = str(engine_dir())
    if path not in sys.path:
        sys.path.insert(0, path)
