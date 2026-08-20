"""Live-gated Snyk plugin smoke, skipped unless the tool and the example plugin are available.

Set SNYK_LIVE_SMOKE=1, have the snyk CLI on PATH and authenticated, and the read-only scan runs
the examples/snyk-gate plugin against the current checkout through the entry-point plugin path.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from adapters.interfaces.security_scanner import DEPENDENCY, ScanTarget

_enabled = os.environ.get("SNYK_LIVE_SMOKE") == "1"
_have_snyk = shutil.which("snyk") is not None
_plugin_root = Path(__file__).resolve().parents[2] / "examples" / "snyk-gate"

pytestmark = pytest.mark.skipif(
    not (_enabled and _have_snyk and _plugin_root.is_dir()),
    reason="set SNYK_LIVE_SMOKE=1 and install and authenticate the snyk CLI to run",
)


async def test_snyk_plugin_scans_the_checkout() -> None:
    if str(_plugin_root) not in sys.path:
        sys.path.insert(0, str(_plugin_root))
    from snyk_gate import SnykScanner  # pyright: ignore[reportMissingImports]

    scanner = SnykScanner()
    findings = await scanner.scan(
        ScanTarget(repo="agentic-dev-platform", ref="HEAD", repo_root=".")
    )
    assert all(f.scanner == "snyk" and f.kind == DEPENDENCY for f in findings)
