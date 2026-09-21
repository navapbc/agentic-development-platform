"""Drop and recreate the agent_platform schema."""

from __future__ import annotations

from database.models import SCHEMA
from sqlalchemy import create_engine, text

from scripts.config import alembic_database_url


def _sync_url() -> str:
    return alembic_database_url()


def main() -> int:
    engine = create_engine(_sync_url(), future=True)
    with engine.begin() as conn:
        print(f"[db-reset] dropping schema {SCHEMA} (CASCADE)")
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    print("[db-reset] schema recreated; run migrations next")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
