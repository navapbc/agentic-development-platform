"""Zip mode resolves an uploaded engine archive with a single wrapper directory."""

from __future__ import annotations

import zipfile
from pathlib import Path

from services.orchestration.gates.engine_source import GateEngineConfig, resolve_engine_source

_ENGINE = Path(__file__).resolve().parents[2] / "examples" / "gate-engines" / "test-policy"


async def test_zip_mode_resolves_an_engine_archive(tmp_path):
    archive = tmp_path / "test-policy.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(p for p in _ENGINE.rglob("*") if p.is_file()):
            bundle.write(path, f"test-policy-1.0.0/{path.relative_to(_ENGINE).as_posix()}")

    resolved = await resolve_engine_source(
        GateEngineConfig(
            enabled=True,
            mode="zip",
            zip_path=str(archive),
            cache_root=str(tmp_path / "cache"),
            review_entrypoint="bin/review",
        )
    )

    assert resolved is not None
    assert resolved.mode == "zip"
    assert resolved.version.startswith("sha256:")
    assert (Path(resolved.path) / "bin" / "review").is_file()
