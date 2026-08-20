"""Live-gated engine smoke, skipped unless the engine and gateway are available.

Set GATE_ENGINE_LIVE_SMOKE=1, have the claude CLI on PATH, a reachable LiteLLM gateway
(LITELLM_BASE_URL, default http://localhost:4000, plus LITELLM_MASTER_KEY), and a local
ai-common-workflows checkout (GATE_ENGINE_LOCAL_PATH, default ../ai-common-workflows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from adapters.interfaces.security_scanner import CRITICAL, HIGH, ScanTarget, is_blocking
from services.orchestration.gates.engine_source import GateEngineConfig
from services.orchestration.gates.gate_engine import AiReviewScanner

_enabled = os.environ.get("GATE_ENGINE_LIVE_SMOKE") == "1"
_have_claude = shutil.which("claude") is not None
_engine_path = Path(
    os.environ.get("GATE_ENGINE_LOCAL_PATH", "../ai-common-workflows")
).expanduser()
_have_engine = _engine_path.is_dir()

pytestmark = pytest.mark.skipif(
    not (_enabled and _have_claude and _have_engine),
    reason=(
        "set GATE_ENGINE_LIVE_SMOKE=1, install the claude CLI, and provide a local "
        "ai-common-workflows checkout via GATE_ENGINE_LOCAL_PATH to run"
    ),
)

_VULNERABLE_SOURCE = (
    "import subprocess\n"
    "\n"
    'DB_PASSWORD = "hunter2-super-secret"\n'
    "\n"
    "def find_user(conn, name):\n"
    "    return conn.execute(\"SELECT * FROM users WHERE name = '\" + name + \"'\")\n"
    "def run(cmd):\n"
    "    return subprocess.run(cmd, shell=True)\n"
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


async def test_ai_review_maps_planted_vulnerabilities(tmp_path: Path) -> None:
    gateway = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not key:
        pytest.skip("set LITELLM_MASTER_KEY to reach the gateway")

    repo = tmp_path / "review-target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base_ref = _git(repo, "rev-parse", "HEAD")
    (repo / "auth.py").write_text(_VULNERABLE_SOURCE, encoding="utf-8")
    _git(repo, "add", "auth.py")
    _git(repo, "commit", "-q", "-m", "add auth")

    scanner = AiReviewScanner(
        name="ai-security-review",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(_engine_path),
            tool="claude",
            review_model="security-review-model",
        ),
        litellm_base_url=gateway,
        litellm_api_key=key,
    )

    findings = await scanner.scan(
        ScanTarget(repo="review-target", ref=base_ref, repo_root=str(repo))
    )

    assert findings, "the engine should report the planted vulnerabilities"
    assert any(f.severity in (CRITICAL, HIGH) and is_blocking(f.severity) for f in findings)
    assert all(f.scanner == "ai-security-review" and f.location for f in findings)
