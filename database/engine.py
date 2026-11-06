"""Create the platform's async database engines with one pool policy.

Each engine holds its own pool, and a process may open several engines, so a task can hold up
to DATABASE_POOL_SIZE plus DATABASE_POOL_MAX_OVERFLOW connections per engine. A deployment
sizes both so every task fits within the database's max_connections.

asyncpg and psycopg read PGUSER, PGPASSWORD, PGSSLMODE, and PGSSLROOTCERT when the URL omits
them, so a deployed task keeps its credentials and TLS mode out of DATABASE_URL.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


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
