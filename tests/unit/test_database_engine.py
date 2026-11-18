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


async def test_the_shared_engine_is_one_per_event_loop_and_url(monkeypatch) -> None:
    import asyncio
    import weakref

    from database import engine as engine_module

    monkeypatch.setattr(engine_module, "_shared", weakref.WeakKeyDictionary())
    built: list[str] = []
    monkeypatch.setattr(
        engine_module, "create_platform_engine", lambda url: built.append(url) or object()
    )

    first = engine_module.shared_platform_engine("postgresql+asyncpg://a/db")
    second = engine_module.shared_platform_engine("postgresql+asyncpg://a/db")
    other_url = engine_module.shared_platform_engine("postgresql+asyncpg://b/db")
    other_loop = await asyncio.to_thread(
        asyncio.run, _shared_engine_on_a_new_loop("postgresql+asyncpg://a/db")
    )

    assert first is second
    assert other_url is not first
    assert other_loop is not first
    assert len(built) == 3


async def _shared_engine_on_a_new_loop(url: str):
    from database.engine import shared_platform_engine

    return shared_platform_engine(url)


def test_no_long_lived_service_creates_an_engine_per_instance() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in sorted((REPO_ROOT / "services").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "create_platform_engine(get_settings().database_url)" in line
    ]

    assert offenders == []
