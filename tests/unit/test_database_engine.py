"""Every platform database engine takes the pool policy from the settings."""

from __future__ import annotations

import re
from pathlib import Path

from database.engine import create_platform_engine
from scripts.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_engine_pool_follows_the_settings(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_POOL_SIZE", "2")
    monkeypatch.setenv("DATABASE_POOL_MAX_OVERFLOW", "3")
    get_settings.cache_clear()
    try:
        engine = create_platform_engine(
            "postgresql+asyncpg://db.example.internal:5432/agent_platform"
        )
        pool = engine.sync_engine.pool
        assert (pool.size(), pool._max_overflow, pool._pre_ping) == (2, 3, True)
    finally:
        get_settings.cache_clear()


def test_no_engine_is_created_outside_the_factory() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for root in ("services", "scripts", "adapters", "database")
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
        if path != REPO_ROOT / "database" / "engine.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\bcreate_async_engine\(", line)
    ]

    assert offenders == []
