"""Database engine and metadata shared by the application and Alembic.

The same models and migrations must run on SQLite (local development, fast tests) and
PostgreSQL (deployment). Constraint names follow a fixed convention so Alembic produces
identical, portable migrations on both.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Dialect, Engine, MetaData, TypeDecorator, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, SessionTransaction, sessionmaker

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
    """The project's sessions: rollback-only protection is attached to this factory only."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event.listen(factory, "before_commit", _refuse_rollback_only_commit)
    event.listen(factory, "after_transaction_end", _clear_rollback_only)
    return factory


# --- Units of work and rollback-only transactions (ARCHITECTURE §3.1, ADR-024) ----------------
#
# A domain operation that has written part of a change and then fails (for example a
# multi-item stock hold that finds one item short) marks its transaction rollback-only before
# raising. From then on the transaction can only be rolled back: catching the exception and
# committing anyway is refused, both by ``unit_of_work`` (which checks the mark itself, so it
# also protects sessions that did not come from ``session_factory``) and by a ``before_commit``
# listener on the project's session factory. The mark is cleared when the outermost
# transaction ends (rollback or close), so a session can be reused afterwards.

_ROLLBACK_ONLY = "atlas_rollback_only"


class RollbackOnlyTransaction(RuntimeError):
    """A commit was attempted on a transaction that a failed domain operation marked rollback-only.

    Always a programming error (an exception was caught and the work carried on). It escapes
    as an unhandled error, so clients get the generic 500 with a reference.
    """


class TransactionAlreadyOpen(RuntimeError):
    """``unit_of_work`` was entered on a session that already has an open transaction."""


def mark_rollback_only(session: Session, reason: str) -> None:
    """Forbid committing the session's current transaction. Call before raising."""
    if not session.in_transaction():
        raise RuntimeError("mark_rollback_only needs an open transaction")
    session.info[_ROLLBACK_ONLY] = reason


def rollback_only_reason(session: Session) -> str | None:
    reason = session.info.get(_ROLLBACK_ONLY)
    return reason if isinstance(reason, str) else None


def _refuse_rollback_only_commit(session: Session) -> None:
    reason = rollback_only_reason(session)
    if reason is not None:
        raise RollbackOnlyTransaction(f"Refusing to commit a rollback-only transaction: {reason}")


def _clear_rollback_only(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:  # the outermost transaction ended (a savepoint ending doesn't count)
        session.info.pop(_ROLLBACK_ONLY, None)


@contextmanager
def unit_of_work(session: Session) -> Iterator[Session]:
    """Run one transaction: commit on success, roll back on ANY exception.

    Refuses to start if the session already has an open transaction, so a unit of work never
    silently joins (and later commits) someone else's partial work. Before committing it checks
    the rollback-only mark itself, so a caught StockShortfall can never be committed.
    """
    if session.in_transaction():
        raise TransactionAlreadyOpen("unit_of_work needs a session with no open transaction")
    session.begin()
    try:
        yield session
        reason = rollback_only_reason(session)
        if reason is not None:
            raise RollbackOnlyTransaction(f"Refusing to commit a rollback-only transaction: {reason}")
        session.commit()
    except BaseException:
        session.rollback()
        session.info.pop(_ROLLBACK_ONLY, None)  # also for sessions without the factory's listener
        raise


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One session per unit of work; committed by the caller, always closed."""
    session = factory()
    try:
        yield session
    finally:
        session.close()
