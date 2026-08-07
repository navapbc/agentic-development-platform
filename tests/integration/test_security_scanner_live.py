"""Live-gated security scanner smokes, skipped unless the tools are installed.

Set SECURITY_SCANNER_LIVE_SMOKE and have semgrep and trivy on PATH to run these against the
current checkout. The scans are read-only.
"""

from __future__ import annotations

import os
import shutil

import pytest
from adapters.interfaces.security_scanner import DEPENDENCY, STATIC, ScanTarget
from adapters.real.security_scanner import RealSemgrepScanner, RealTrivyScanner

_enabled = bool(os.getenv("SECURITY_SCANNER_LIVE_SMOKE"))
_have_semgrep = shutil.which("semgrep") is not None
_have_trivy = shutil.which("trivy") is not None

pytestmark = pytest.mark.skipif(
    not _enabled,
    reason="set SECURITY_SCANNER_LIVE_SMOKE and install semgrep and trivy to run",
)


@pytest.mark.skipif(not _have_semgrep, reason="semgrep not installed")
async def test_semgrep_scans_the_checkout() -> None:
    scanner = RealSemgrepScanner.from_settings()
    target = ScanTarget(repo="agentic-dev-platform", ref="HEAD", repo_root=".")
    findings = await scanner.scan(target)
    assert all(f.kind == STATIC for f in findings)


@pytest.mark.skipif(not _have_trivy, reason="trivy not installed")
async def test_trivy_scans_the_checkout() -> None:
    scanner = RealTrivyScanner.from_settings()
    target = ScanTarget(repo="agentic-dev-platform", ref="HEAD", repo_root=".")
    findings = await scanner.scan(target)
    assert all(f.kind == DEPENDENCY for f in findings)
