"""Resolver check against a GitHub archive of ai-common-workflows, when present.

Download the archive for testing with:
  gh api repos/navapbc/ai-common-workflows/zipball/<ref> > \
    .agent-context/engine-cache/uploads/ai-common-workflows-<short>.zip
"""

from __future__ import annotations

from pathlib import Path

import pytest
from services.orchestration.gates.engine_source import GateEngineConfig, resolve_engine_source

_UPLOADS = Path(__file__).resolve().parents[2] / ".agent-context" / "engine-cache" / "uploads"
_ARCHIVES = sorted(_UPLOADS.glob("*.zip")) if _UPLOADS.is_dir() else []


@pytest.mark.skipif(not _ARCHIVES, reason="no ai-common-workflows archive is cached for testing")
async def test_zip_mode_resolves_a_github_archive(tmp_path):
    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="zip",
            zip_path=str(_ARCHIVES[0]),
            cache_root=str(tmp_path / "cache"),
        )
    )

    assert resolved is not None
    assert resolved.mode == "zip"
    assert resolved.version.startswith("sha256:")
    entrypoint = (
        Path(resolved.path) / "engines" / "security-compliance-review" / "harness" / "ai-pr-review"
    )
    assert entrypoint.is_file()
