"""Create the platform's async database engines with one pool policy.

Each engine holds its own pool, so a task can hold up to DATABASE_POOL_SIZE plus
DATABASE_POOL_MAX_OVERFLOW connections per engine. A deployment sizes both so every task fits
within the database's max_connections. Long-lived services take shared_platform_engine, so a
process holds one pool. create_platform_engine is for a caller that disposes its engine.

asyncpg and psycopg read PGUSER, PGPASSWORD, PGSSLMODE, and PGSSLROOTCERT when the URL omits
them, so a deployed task keeps its credentials and TLS mode out of DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import weakref

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_shared: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncEngine]] = (
    weakref.WeakKeyDictionary()
)


def create_platform_engine(database_url: str) -> AsyncEngine:
    from scripts.config import get_settings

    settings = get_settings()
    return create_async_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
    )


def shared_platform_engine(database_url: str | None = None) -> AsyncEngine:
    """Return the process's engine for database_url on the running event loop.

    An engine that is dropped without dispose keeps its pooled connections open until the
    cyclic garbage collector frees it, which in a worker can take hours, so an engine per
    activity exhausts max_connections. asyncpg connections belong to the loop that opened them,
    which is why each loop has its own engine.
    """
    from scripts.config import get_settings

    url = database_url or get_settings().database_url
    engines = _shared.setdefault(asyncio.get_running_loop(), {})
    engine = engines.get(url)
    if engine is None:
        engine = engines[url] = create_platform_engine(url)
    return engine
