"""Alembic migration environment for the agent_platform schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from database.models import SCHEMA, metadata
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_db_url = os.environ.get(
    "ALEMBIC_DATABASE_URL",
    "postgresql+psycopg://platform:change-me-locally@localhost:5432/agent_platform",
)
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = metadata


def _include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and getattr(obj, "schema", None) not in (SCHEMA, None))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        version_table_schema=SCHEMA,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Schema must exist before Alembic creates its version table there.
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=SCHEMA,
            include_object=_include_object,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
