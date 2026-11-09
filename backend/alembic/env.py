"""Alembic environment: runs migrations against SQLite or PostgreSQL."""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, Engine, event

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


def _check_sqlite_foreign_keys(ctx: Any, **_kwargs: Any) -> None:
    """After each revision, inside its transaction and before its commit (``on_version_apply``):
    refuse to commit a revision that leaves a dangling reference. Raising rolls it back."""
    violations = ctx.connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"migration left {len(violations)} foreign key violation(s)")


def _configure(connection: Connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most constraints in place; batch mode recreates tables safely.
        render_as_batch=sqlite,
        compare_type=True,
        on_version_apply=[_check_sqlite_foreign_keys] if sqlite else [],
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


def _prepare_sqlite_migration_engine(engine: Engine) -> None:
    """Make each SQLite revision one real transaction, with foreign keys off during it.

    Batch mode rebuilds a table by creating a copy, dropping the original and renaming the copy.
    SQLite refuses to drop a table that other tables reference (an implicit DELETE that RESTRICT
    foreign keys reject) unless foreign keys are off, and that pragma only takes effect outside a
    transaction. Python's sqlite3 driver would also open its transaction only at the first data
    statement, autocommitting the DDL before it. So, on this migration-only engine (SQLAlchemy's
    documented pysqlite recipe): the driver never opens transactions itself, SQLAlchemy emits
    BEGIN, and foreign keys are switched off on the raw connection before any transaction.
    Every reference is checked before each revision commits (``_check_sqlite_foreign_keys``),
    and enforcement is switched back on before the connection is released (SQLite's documented
    ALTER TABLE procedure).
    """

    @event.listens_for(engine, "connect")
    def _manual_transactions(dbapi_connection: Any, _record: Any) -> None:
        # Runs after create_db_engine's own connect hook (which turned foreign keys on).
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")


def _restore_sqlite_foreign_keys(connection: Connection) -> None:
    if connection.in_transaction():
        connection.rollback()  # never commit leftovers; each revision committed (or not) itself
    connection.connection.driver_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[union-attr]


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    sqlite = engine.dialect.name == "sqlite"
    if sqlite:
        _prepare_sqlite_migration_engine(engine)
    try:
        with engine.connect() as connection:
            try:
                _configure(connection)
                with context.begin_transaction():
                    context.run_migrations()
            finally:
                if sqlite:
                    _restore_sqlite_foreign_keys(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
