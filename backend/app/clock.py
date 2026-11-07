"""Time in UTC, consistent on SQLite and PostgreSQL, and controllable in tests.

SQLite does not store time zones: a ``DateTime(timezone=True)`` column comes back naive,
while PostgreSQL returns an aware value. Comparing naive with aware datetimes raises
``TypeError``. ``as_utc`` normalises both to aware UTC, and ``app.db.UTCDateTime`` applies it
to every value read from or written to the new M2 columns.

Code that makes time-dependent decisions (reservation and cart expiry) takes a ``Clock``
instead of calling ``datetime.now``: ``SystemClock`` in the app, ``FixedClock`` in tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def as_utc(value: datetime) -> datetime:
    """Aware UTC. A naive value is taken to be UTC already (that is how SQLite returns it)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time as an aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to. For tests."""

    def __init__(self, start: datetime) -> None:
        self._now = as_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = as_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now
