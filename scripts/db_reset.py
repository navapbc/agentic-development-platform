"""Drop and recreate the agent_platform schema."""

from __future__ import annotations

from database.models import SCHEMA
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from scripts.config import alembic_database_url


def _sync_url() -> str:
    return alembic_database_url()


def main() -> int:
    engine = create_engine(_sync_url(), future=True)
    with engine.begin() as conn:
        print(f"[db-reset] dropping schema {SCHEMA} (CASCADE)")
        conn.execute(DropSchema(SCHEMA, cascade=True, if_exists=True))
        conn.execute(CreateSchema(SCHEMA))
    print("[db-reset] schema recreated; run migrations next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
