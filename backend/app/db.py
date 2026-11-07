"""Database engine and metadata shared by the application and Alembic.

The same models and migrations must run on SQLite (local development, fast tests) and
PostgreSQL (deployment). Constraint names follow a fixed convention so Alembic produces
identical, portable migrations on both.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Dialect, Engine, MetaData, TypeDecorator, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.clock import as_utc

# Multi-column constraints are named after ALL their columns (column_0_N_name), so two
# constraints that start with the same column can never collide. Changed in M1, before any
# named constraint existed in a deployed database.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Timezone-aware UTC now; all timestamps are written from Python for portability."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that always returns aware UTC, on SQLite and PostgreSQL.

    Writes must be timezone-aware (a naive value is rejected, because its meaning is
    ambiguous) and are converted to UTC first: SQLite would otherwise store a non-UTC wall
    time without its offset. Reads are normalised with ``as_utc``. Bound parameters in
    comparisons (``expires_at <= :now``) go through the same conversion.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else as_utc(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _ensure_sqlite_directory(url: str) -> None:
    database = make_url(url).database
    if database and database != ":memory:" and not database.startswith("file:"):
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_db_engine(url: str) -> Engine:
    """Create an engine with settings suited to the database dialect.

    ``hide_parameters``: SQL errors (and so logged tracebacks) show statements without bound
    values, which will include customer data from M2 onwards.
    """
    if url.startswith("sqlite"):
        _ensure_sqlite_directory(url)
        engine = create_engine(url, connect_args={"check_same_thread": False}, hide_parameters=True)
        event.listen(engine, "connect", _configure_sqlite)
        return engine

    # Managed PostgreSQL (e.g. Neon) suspends idle compute and drops connections, so check
    # connections before use and recycle them well inside the provider's idle timeout.
    return create_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 10},
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One session per unit of work; committed by the caller, always closed."""
    session = factory()
    try:
        yield session
    finally:
        session.close()
