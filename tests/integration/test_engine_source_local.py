"""Local mode resolves an engine directory inside a git checkout to its commit."""

from __future__ import annotations

from pathlib import Path

from services.orchestration.gates.engine_source import GateEngineConfig, resolve_engine_source

_ENGINE = Path(__file__).resolve().parents[2] / "examples" / "gate-engines" / "security-policy"


async def test_local_mode_resolves_the_example_engine():
    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True, mode="local", local_path=str(_ENGINE), review_entrypoint="bin/scan"
        )
    )

    assert resolved is not None
    assert resolved.mode == "local"
    assert len(resolved.version) == 40
    assert (Path(resolved.path) / "bin" / "scan").is_file()
