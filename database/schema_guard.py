"""Startup check that the platform schema is at the migration head this image carries.

Services that read the platform tables wait here before serving. Migrations run as a separate
one-shot task, so a service never needs DDL rights and never races another service to migrate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError
from sqlalchemy import column, select, table, text
from sqlalchemy.ext.asyncio import create_async_engine

from database.models import SCHEMA

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
POLL_SECONDS = 5.0
LOG_EVERY_SECONDS = 30.0

log = logging.getLogger("schema-guard")

_alembic_version = table("alembic_version", column("version_num"), schema=SCHEMA)


class SchemaState(StrEnum):
    CURRENT = "current"
    BEHIND = "behind"
    UNKNOWN = "unknown"


class SchemaAheadOfImage(SystemExit):
    """The database holds a revision this image does not carry, so waiting cannot resolve it."""


def migration_script() -> ScriptDirectory:
    return ScriptDirectory(str(MIGRATIONS_DIR))


def classify(applied: Iterable[str], script: ScriptDirectory) -> SchemaState:
    """Compare the applied revisions with the heads the image carries."""
    applied_set = set(applied)
    if applied_set == set(script.get_heads()):
        return SchemaState.CURRENT
    for revision in applied_set:
        try:
            if script.get_revision(revision) is None:
                return SchemaState.UNKNOWN
        except (CommandError, RevisionError):
            return SchemaState.UNKNOWN
    return SchemaState.BEHIND


async def applied_revisions(database_url: str) -> set[str]:
    """Read the applied revisions, treating a missing version table as an empty schema."""
    engine = create_async_engine(database_url, future=True)
    try:
        async with engine.connect() as conn:
            table = await conn.scalar(
                text("SELECT to_regclass(:name)"), {"name": f"{SCHEMA}.alembic_version"}
            )
            if table is None:
                return set()
            rows = await conn.execute(select(_alembic_version.c.version_num))
            return {str(row[0]) for row in rows}
    finally:
        await engine.dispose()


async def wait_for_schema_head(
    fetch_applied: Callable[[], Awaitable[set[str]]],
    *,
    script: ScriptDirectory | None = None,
    poll_seconds: float = POLL_SECONDS,
    log_every_seconds: float = LOG_EVERY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Return once the schema is at head, wait while it is behind, and exit when it is ahead."""
    script = script or migration_script()
    heads = sorted(script.get_heads())
    last_logged: float | None = None
    while True:
        try:
            applied = await fetch_applied()
        except Exception as exc:
            reason = f"could not read the schema revision ({exc.__class__.__name__} {exc})"
            action = (
                "The database did not answer, so a migration is not what is missing. Check that "
                "Postgres is up with docker compose ps postgres, and that DATABASE_URL names the "
                "host and port it listens on."
            )
        else:
            state = classify(applied, script)
            if state is SchemaState.CURRENT:
                log.info("schema is at head %s", ", ".join(heads))
                return
            if state is SchemaState.UNKNOWN:
                raise SchemaAheadOfImage(
                    f"the database schema is at {', '.join(sorted(applied))}, which this image "
                    f"does not carry. Its head is {', '.join(heads)}. A newer checkout migrated "
                    "the database. Run that checkout, or restore the database from a backup."
                )
            current = ", ".join(sorted(applied)) or "no revision"
            expected = ", ".join(heads)
            reason = f"the database schema is at {current} and this image expects {expected}"
            action = "Run inv db-migrate."
        now = clock()
        if last_logged is None or now - last_logged >= log_every_seconds:
            log.warning("waiting for the platform schema. %s. %s", reason, action)
            last_logged = now
        await sleep(poll_seconds)


async def wait_for_platform_schema(database_url: str) -> None:
    await wait_for_schema_head(lambda: applied_revisions(database_url))
