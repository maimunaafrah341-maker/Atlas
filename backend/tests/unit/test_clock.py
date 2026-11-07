from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.clock import FixedClock, SystemClock, as_utc
from app.db import UTCDateTime

IST = timezone(timedelta(hours=5, minutes=30))


def test_as_utc_treats_naive_as_utc() -> None:
    assert as_utc(datetime(2026, 11, 7, 9, 0)) == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    assert as_utc(datetime(2026, 11, 7, 9, 0)).tzinfo is UTC


def test_as_utc_converts_other_offsets() -> None:
    converted = as_utc(datetime(2026, 11, 7, 14, 30, tzinfo=IST))
    assert converted == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    assert converted.tzinfo is UTC


def test_system_clock_is_aware_utc() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_fixed_clock_only_moves_when_told() -> None:
    clock = FixedClock(datetime(2026, 11, 7, 9, 0, tzinfo=UTC))
    assert clock.now() == clock.now() == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    assert clock.advance(timedelta(minutes=15)) == datetime(2026, 11, 7, 9, 15, tzinfo=UTC)
    clock.set(datetime(2026, 11, 7, 20, 0, tzinfo=IST))
    assert clock.now() == datetime(2026, 11, 7, 14, 30, tzinfo=UTC)


def test_utc_datetime_rejects_naive_writes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UTCDateTime().process_bind_param(datetime(2026, 11, 7, 9, 0), dialect=None)  # type: ignore[arg-type]


def test_utc_datetime_converts_writes_to_utc() -> None:
    bound = UTCDateTime().process_bind_param(datetime(2026, 11, 7, 14, 30, tzinfo=IST), dialect=None)  # type: ignore[arg-type]
    assert bound == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    assert bound is not None
    assert bound.utcoffset() == timedelta(0)
