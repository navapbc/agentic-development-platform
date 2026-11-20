"""Live-gated smoke of the example test-policy engine through the model gateway.

Set GATE_ENGINE_LIVE_SMOKE=1 and LITELLM_MASTER_KEY, with a LiteLLM gateway at LITELLM_BASE_URL
(default http://localhost:4000) that serves GATE_ENGINE_LIVE_MODEL (default claude-opus-5-5).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from adapters.interfaces.security_scanner import MEDIUM, ScanTarget
from services.orchestration.gates.engine_source import GateEngineConfig
from services.orchestration.gates.gate_engine import AiReviewScanner

_ENGINE = Path(__file__).resolve().parents[2] / "examples" / "gate-engines" / "test-policy"

pytestmark = pytest.mark.skipif(
    os.environ.get("GATE_ENGINE_LIVE_SMOKE") != "1" or not os.environ.get("LITELLM_MASTER_KEY"),
    reason="set GATE_ENGINE_LIVE_SMOKE=1 and LITELLM_MASTER_KEY to reach the gateway",
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


async def test_the_test_policy_engine_adds_model_suggestions(tmp_path: Path) -> None:
    repo = tmp_path / "review-target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base_ref = _git(repo, "rev-parse", "HEAD")
    (repo / "billing.py").write_text(
        "def charge(amount, rate):\n"
        "    if amount < 0:\n"
        "        raise ValueError('amount must not be negative')\n"
        "    return round(amount * (1 + rate), 2)\n",
        encoding="utf-8",
    )

    scanner = AiReviewScanner(
        name="test-policy",
        config=GateEngineConfig(
            enabled=True,
            mode="local",
            local_path=str(_ENGINE),
            review_model=os.environ.get("GATE_ENGINE_LIVE_MODEL", "claude-opus-5-5"),
            review_entrypoint="bin/review",
        ),
        litellm_base_url=os.environ.get("LITELLM_BASE_URL", "http://localhost:4000"),
        litellm_api_key=os.environ["LITELLM_MASTER_KEY"],
    )

    findings = await scanner.scan(
        ScanTarget(repo="review-target", ref=base_ref, base_ref=base_ref, repo_root=str(repo))
    )

    assert [(f.location, f.severity) for f in findings] == [("billing.py:1", MEDIUM)]
    assert "Suggested test cases:" in findings[0].detail
