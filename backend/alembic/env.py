"""Alembic environment: runs migrations against SQLite or PostgreSQL."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.config import get_settings
from app.db import Base, create_db_engine

config = context.config

# Only configure logging from alembic.ini when run from the command line; the application
# and tests configure their own logging.
if config.config_file_name is not None and not config.attributes.get("database_url"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url")
    return str(url) if url else get_settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most constraints in place; batch mode recreates tables safely.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _sqlite_foreign_keys(connection: Connection, *, enabled: bool) -> None:
    # Only effective outside a transaction, so it is set before the migration transaction starts
    # and restored after it ends.
    connection.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    connection.commit()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    try:
        with engine.connect() as connection:
            sqlite = connection.dialect.name == "sqlite"
            if sqlite:
                # Batch mode rebuilds a table by creating a copy, dropping the original and
                # renaming the copy. SQLite refuses to drop a table that other tables reference
                # (an implicit DELETE that RESTRICT foreign keys reject) unless foreign keys are
                # off, so they are off for the migration and every reference is checked before
                # the transaction commits (SQLite's documented ALTER TABLE procedure).
                _sqlite_foreign_keys(connection, enabled=False)
            try:
                _configure(connection)
                with context.begin_transaction():
                    context.run_migrations()
                    if sqlite:
                        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                        if violations:
                            raise RuntimeError(f"migration left {len(violations)} foreign key violation(s)")
            finally:
                if sqlite:
                    _sqlite_foreign_keys(connection, enabled=True)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
