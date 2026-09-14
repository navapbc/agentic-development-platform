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


def _guard_host_apply() -> None:
    """Refuse to apply migrations from outside the db-migrate container.

    Migrations are applied only through the db-migrate container so the applied
    scripts always match the deployed image and no host run can advance the
    database past what the container carries. The db-migrate service sets
    DB_MIGRATE_IN_CONTAINER. Authoring commands such as revision autogenerate stay
    available on the host because they do not change the database revision. This
    guard covers the alembic command line and does not intercept the alembic Python
    API.
    """
    if os.environ.get("DB_MIGRATE_IN_CONTAINER") == "1":
        return
    cmd_opts = getattr(config, "cmd_opts", None)
    cmd = getattr(cmd_opts, "cmd", None)
    command = cmd[0].__name__ if cmd else ""
    if command in {"upgrade", "downgrade", "stamp"}:
        raise SystemExit(
            f"Refusing to run 'alembic {command}' on the host. Apply migrations with "
            "'inv db-migrate', which runs the db-migrate container. See AGENTS.md."
        )


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
    _guard_host_apply()
    run_migrations_offline()
else:
    _guard_host_apply()
    run_migrations_online()
