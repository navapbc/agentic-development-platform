"""Live-gated resolver check against the checked-out ai-common-workflows sibling."""

from __future__ import annotations

from pathlib import Path

import pytest
from services.orchestration.gates.engine_source import GateEngineConfig, resolve_engine_source

_SIBLING = Path(__file__).resolve().parents[2].parent / "ai-common-workflows"


@pytest.mark.skipif(
    not (_SIBLING / ".git").is_dir(),
    reason="the ai-common-workflows sibling checkout is not present",
)
async def test_local_mode_resolves_the_sibling_checkout():
    resolved = await resolve_engine_source(
        GateEngineConfig(enabled=True, mode="local", local_path=str(_SIBLING))
    )

    assert resolved is not None
    assert resolved.mode == "local"
    assert len(resolved.version) == 40
    assert (Path(resolved.path) / "engines" / "_common").is_dir()
