"""Deterministic PostgreSQL concurrency tests for M2.2 (ARCHITECTURE §3.1, ADR-024).

PostgreSQL only: SQLite has a single writer, so these races can't happen there.

How the races are made deterministic: a separate "blocker" connection holds the row lock the
operations under test will need. The operations start in threads; the test then waits (by
polling ``pg_stat_activity`` for the condition, not a fixed sleep) until the expected number
of backends are actually waiting on a lock. Only then does the blocker release, so every
operation is guaranteed to have reached the contended statement concurrently. Every
connection has ``lock_timeout`` and ``statement_timeout`` set, so a hang becomes a failure,
and PostgreSQL's deadlock detector turns a deadlock into an error the test sees.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.db import create_db_engine, session_factory, unit_of_work
from app.domain.cart.models import Cart, CartItem
from app.domain.cart.service import remove_line, set_quantity
from app.domain.catalog.models import Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import (
    ReservationShortfall,
    _reserve_locked,
    expire_reservation,
    release_cart_reservation,
    reserve_cart,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.reservation_builders import NOW, TTL

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="real concurrency needs PostgreSQL (TEST_DATABASE_URL)"),
]

WAIT_TIMEOUT = 15.0  # seconds to reach the expected number of lock waiters
RESULT_TIMEOUT = 30.0


# --- infrastructure ----------------------------------------------------------------------------


def _timeouts(dbapi_connection: Any, _record: Any) -> None:
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '10s'")
        cursor.execute("SET statement_timeout = '20s'")
    dbapi_connection.commit()


@pytest.fixture
def engine(migrated_database_url: str) -> Iterator[Engine]:
    engine = create_db_engine(migrated_database_url)
    event.listen(engine, "connect", _timeouts)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return session_factory(engine)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


class Race:
    """Runs operations in threads, each with its own session; collects results or errors."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.futures: list[Future[Any]] = []

    def start(self, operation: Callable[[Session], Any]) -> Future[Any]:
        def run() -> Any:
            with self.factory() as session:
                return operation(session)

        future = self.pool.submit(run)
        self.futures.append(future)
        return future

    def outcomes(self) -> list[Any]:
        """Each operation's result, or the exception it raised (deadlocks included)."""
        results: list[Any] = []
        for future in self.futures:
            try:
                results.append(future.result(timeout=RESULT_TIMEOUT))
            except Exception as error:  # the test inspects every outcome
                results.append(error)
        return results

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def race(factory: sessionmaker[Session]) -> Iterator[Race]:
    race = Race(factory)
    yield race
    race.close()


def wait_for_lock_waiters(engine: Engine, count: int, futures: list[Future[Any]] | None = None) -> None:
    """Wait until ``count`` other backends are blocked on a lock (a condition, not a sleep)."""
    deadline = time.monotonic() + WAIT_TIMEOUT
    with engine.connect() as connection:
        while True:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
            connection.rollback()  # pg_stat_activity is a per-transaction snapshot
            if waiting >= count:
                return
            failed = [f.exception() for f in futures or [] if f.done() and f.exception() is not None]
            if failed:
                raise AssertionError(f"an operation failed before blocking: {failed[0]!r}")
            if time.monotonic() > deadline:
                raise AssertionError(f"only {waiting} of {count} operations reached the lock")
            time.sleep(0.01)  # polling interval for the condition above


def blocker(engine: Engine, *statements: str, **params: Any) -> Connection:
    """A connection holding row locks until the test commits or rolls it back."""
    connection = engine.connect()
    connection.begin()
    for statement in statements:
        connection.execute(text(statement), params)
    return connection


def setup(factory: sessionmaker[Session], build: Callable[[Session], Any]) -> Any:
    with factory() as session, unit_of_work(session):
        return build(session)


def ids(factory: sessionmaker[Session], *skus: str) -> list[int]:
    with factory() as session:
        found = dict(session.execute(select(Variant.sku, Variant.id).where(Variant.sku.in_(skus))).all())
    return [found[sku] for sku in skus]


def new_cart(factory: sessionmaker[Session], clock: FixedClock, lines: dict[str, int]) -> int:
    with factory() as session:
        cart_id: int | None = None
        for sku, quantity in lines.items():
            cart_id = set_quantity(session, cart_id, sku, quantity, clock=clock).cart.cart_id
    assert cart_id is not None
    return cart_id


def stock(factory: sessionmaker[Session], sku: str) -> tuple[int, int]:
    with factory() as session:
        row = session.execute(select(Variant.stock_on_hand, Variant.stock_reserved).where(Variant.sku == sku)).one()
    return row.stock_on_hand, row.stock_reserved


def statuses(factory: sessionmaker[Session]) -> list[str]:
    with factory() as session:
        return list(session.execute(select(Reservation.status).order_by(Reservation.id)).scalars())


def movement_reasons(factory: sessionmaker[Session]) -> list[str]:
    with factory() as session:
        return list(session.execute(select(InventoryMovement.reason).order_by(InventoryMovement.id)).scalars())


def check_invariants(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        assert_inventory_invariants(session)


def no_errors(outcomes: list[Any]) -> None:
    errors = [o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, ReservationShortfall)]
    assert errors == [], errors


# --- P1: carts competing for the last unit -----------------------------------------------------


def test_p1_carts_competing_for_the_last_unit(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "last", variants=[V(sku="LAST", on_hand=1)]))
    carts = [new_cart(factory, clock, {"LAST": 1}) for _ in range(4)]
    (variant_id,) = ids(factory, "LAST")

    hold = blocker(engine, "SELECT id FROM variants WHERE id = :id FOR NO KEY UPDATE", id=variant_id)
    for cart_id in carts:
        race.start(lambda s, c=cart_id: reserve_cart(s, c, clock=clock, ttl=TTL))
    wait_for_lock_waiters(engine, 4, race.futures)  # all four are at the stock UPDATE
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    no_errors(outcomes)
    assert sum(not isinstance(o, BaseException) and o.created for o in outcomes) == 1
    assert sum(isinstance(o, ReservationShortfall) for o in outcomes) == 3
    assert stock(factory, "LAST") == (1, 1)
    assert statuses(factory) == ["active"]  # failed attempts left no reservation rows
    check_invariants(factory)


# --- P2: concurrent reservation requests for the same cart (idempotent retries) -------------------


def test_p2_parallel_reserve_requests_for_one_cart_create_one_reservation(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p2", variants=[V(sku="P2", on_hand=5)]))
    cart_id = new_cart(factory, clock, {"P2": 2})

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    for _ in range(4):
        race.start(lambda s: reserve_cart(s, cart_id, clock=clock, ttl=TTL))
    wait_for_lock_waiters(engine, 4, race.futures)  # all four at the version compare-and-set
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    no_errors(outcomes)
    assert sum(o.created for o in outcomes) == 1
    assert len({o.reservation.public_id for o in outcomes}) == 1  # losers returned the winner's
    assert stock(factory, "P2") == (5, 2)
    assert movement_reasons(factory) == ["reservation_hold"]
    check_invariants(factory)


# --- P3: a lost compare-and-set against a cart change ---------------------------------------------


def test_p3_reserve_loses_to_a_committed_cart_change(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p3", variants=[V(sku="P3", on_hand=5)]))
    cart_id = new_cart(factory, clock, {"P3": 1})

    change = blocker(engine, "UPDATE carts SET version = version + 1 WHERE id = :id", id=cart_id)
    future = race.start(lambda s: reserve_cart(s, cart_id, clock=clock, ttl=TTL))
    wait_for_lock_waiters(engine, 1, race.futures)
    change.commit()  # the reservation's WHERE version = :seen is re-checked: no row
    change.close()

    with pytest.raises(Exception, match="changed while it was being reserved"):
        future.result(timeout=RESULT_TIMEOUT)
    assert statuses(factory) == []
    assert stock(factory, "P3") == (5, 0)
    check_invariants(factory)


# --- P4: an edit waiting for the cart sees a reservation committed meanwhile ----------------------


def test_p4_edit_after_waiting_releases_the_reservation_committed_meanwhile(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p4", variants=[V(sku="P4-S", size="S", on_hand=5), V(sku="P4-M")]))
    cart_id = new_cart(factory, clock, {"P4-S": 2})

    # An in-flight reservation: the main reservation transaction, not yet committed.
    reserving = factory()
    version = reserving.execute(select(Cart.version).where(Cart.id == cart_id)).scalar_one()
    reservation_id = _reserve_locked(reserving, cart_id, seen=version, now=NOW, ttl=TTL)

    edit = race.start(lambda s: set_quantity(s, cart_id, "P4-S", 3, clock=clock))
    wait_for_lock_waiters(engine, 1, race.futures)  # the edit waits for the cart lock
    reserving.commit()
    reserving.close()

    change = edit.result(timeout=RESULT_TIMEOUT)
    public_id = statuses_by_id(factory)[reservation_id][0]
    assert change.ended_reservation is not None
    assert change.ended_reservation.public_id == public_id  # its lookup after the lock saw it
    assert statuses(factory) == ["released"]
    assert stock(factory, "P4-S") == (5, 0)
    check_invariants(factory)


def statuses_by_id(factory: sessionmaker[Session]) -> dict[int, tuple[str, str]]:
    with factory() as session:
        rows = session.execute(select(Reservation.id, Reservation.public_id, Reservation.status)).all()
    return {row.id: (row.public_id, row.status) for row in rows}


# --- P5: customer release racing an expiry sweep -------------------------------------------------


def test_p5_release_and_expiry_end_a_reservation_exactly_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p5", variants=[V(sku="P5", on_hand=5)]))
    cart_id = new_cart(factory, clock, {"P5": 2})
    with factory() as session:
        reserve_cart(session, cart_id, clock=clock, ttl=TTL)
    reservation_id = next(iter(statuses_by_id(factory)))
    late = FixedClock(NOW + TTL)  # overdue for both

    hold = blocker(engine, "SELECT id FROM reservations WHERE id = :id FOR NO KEY UPDATE", id=reservation_id)
    released = race.start(lambda s: release_cart_reservation(s, cart_id, clock=late))
    expired = race.start(lambda s: expire_reservation(s, reservation_id, clock=late))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    no_errors(outcomes)
    release_won = released.result() is not None
    expiry_won = expired.result()
    assert release_won != expiry_won  # exactly one transition
    assert statuses(factory) == ["expired"]  # overdue: both paths record expiry
    assert movement_reasons(factory) == ["reservation_hold", "reservation_expiry"]  # released once
    assert stock(factory, "P5") == (5, 0)
    check_invariants(factory)


# --- P6/P7: concurrent operations on the same cart -----------------------------------------------


def test_p6_release_and_edit_on_one_cart_end_the_reservation_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p6", variants=[V(sku="P6", on_hand=5)]))
    cart_id = new_cart(factory, clock, {"P6": 2})
    with factory() as session:
        reserve_cart(session, cart_id, clock=clock, ttl=TTL)

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    released = race.start(lambda s: release_cart_reservation(s, cart_id, clock=clock))
    edited = race.start(lambda s: set_quantity(s, cart_id, "P6", 3, clock=clock))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    no_errors(race.outcomes())
    ended_by_release = released.result() is not None
    ended_by_edit = edited.result().ended_reservation is not None
    assert ended_by_release != ended_by_edit  # whichever went first ended it; the other saw nothing
    assert statuses(factory) == ["released"]
    assert movement_reasons(factory) == ["reservation_hold", "reservation_release"]
    assert stock(factory, "P6") == (5, 0)
    check_invariants(factory)


def test_p7_concurrent_edits_to_one_cart_serialise(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p7", variants=[V(sku="P7-S", size="S"), V(sku="P7-M")]))
    cart_id = new_cart(factory, clock, {"P7-S": 1})
    with factory() as session:
        reserve_cart(session, cart_id, clock=clock, ttl=TTL)
        version = session.execute(select(Cart.version).where(Cart.id == cart_id)).scalar_one()

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    edits = [
        race.start(lambda s: set_quantity(s, cart_id, "P7-S", 2, clock=clock)),
        race.start(lambda s: set_quantity(s, cart_id, "P7-M", 1, clock=clock)),
        race.start(lambda s: remove_line(s, cart_id, "P7-S", clock=clock)),
    ]
    wait_for_lock_waiters(engine, 3, race.futures)
    hold.rollback()
    hold.close()

    no_errors(race.outcomes())
    assert sum(edit.result().ended_reservation is not None for edit in edits) == 1  # released once
    assert movement_reasons(factory) == ["reservation_hold", "reservation_release"]
    with factory() as session:
        final_version = session.execute(select(Cart.version).where(Cart.id == cart_id)).scalar_one()
        lines = dict(
            session.execute(
                select(Variant.sku, CartItem.quantity)
                .select_from(CartItem)
                .join(Variant, Variant.id == CartItem.variant_id)
                .where(CartItem.cart_id == cart_id)
            ).all()
        )
    assert "P7-M" in lines
    # Every edit that changed something advanced the version once; none was lost.
    changed = sum(edit.result().cart.version > version for edit in edits)
    assert final_version == version + changed
    check_invariants(factory)


# --- P8: overlapping multi-variant reservations in opposite orders --------------------------------


def test_p8_opposite_order_multi_variant_reservations_do_not_deadlock(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p8", variants=[V(sku="X", size="S", on_hand=1), V(sku="Y", on_hand=1)]))
    first = new_cart(factory, clock, {"X": 1, "Y": 1})
    second = new_cart(factory, clock, {"Y": 1, "X": 1})  # lines added in the opposite order
    x, y = ids(factory, "X", "Y")

    hold = blocker(engine, "SELECT id FROM variants WHERE id IN (:x, :y) ORDER BY id FOR NO KEY UPDATE", x=x, y=y)
    race.start(lambda s: reserve_cart(s, first, clock=clock, ttl=TTL))
    race.start(lambda s: reserve_cart(s, second, clock=clock, ttl=TTL))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    no_errors(outcomes)  # in particular, no deadlock_detected
    assert sum(not isinstance(o, BaseException) and o.created for o in outcomes) == 1
    shortfalls = [o for o in outcomes if isinstance(o, ReservationShortfall)]
    assert len(shortfalls) == 1
    assert shortfalls[0].details == [{"sku": "X"}, {"sku": "Y"}]
    assert (stock(factory, "X"), stock(factory, "Y")) == ((1, 1), (1, 1))
    check_invariants(factory)


# --- P9: sweeps between carts, and sweeps against customer operations -----------------------------


def test_p9_two_carts_sweeping_each_other_do_not_deadlock(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, lambda s: make_product(s, "p9", variants=[V(sku="SX", size="S", on_hand=2), V(sku="SY", on_hand=2)]))
    first = new_cart(factory, clock, {"SX": 1, "SY": 1})
    second = new_cart(factory, clock, {"SY": 1, "SX": 1})
    with factory() as session:
        reserve_cart(session, first, clock=clock, ttl=TTL)
        reserve_cart(session, second, clock=clock, ttl=TTL)
    late = FixedClock(NOW + TTL)  # both reservations overdue; each cart will sweep the other's
    x, y = ids(factory, "SX", "SY")

    hold = blocker(engine, "SELECT id FROM variants WHERE id IN (:x, :y) ORDER BY id FOR NO KEY UPDATE", x=x, y=y)
    race.start(lambda s: reserve_cart(s, first, clock=late, ttl=TTL))
    race.start(lambda s: reserve_cart(s, second, clock=late, ttl=TTL))
    wait_for_lock_waiters(engine, 2, race.futures)  # both are releasing an overdue reservation
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    no_errors(outcomes)
    assert all(o.created for o in outcomes)
    assert statuses(factory) == ["expired", "expired", "active", "active"]
    assert movement_reasons(factory).count("reservation_expiry") == 4  # 2 reservations x 2 variants, once each
    assert (stock(factory, "SX"), stock(factory, "SY")) == ((2, 2), (2, 2))
    check_invariants(factory)


def test_p10_sweep_and_customer_edit_on_the_swept_cart(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    # Cart A's overdue reservation is swept by cart B's reservation while A's owner edits A.
    setup(factory, lambda s: make_product(s, "p10", variants=[V(sku="SW", on_hand=1)]))
    owner = new_cart(factory, clock, {"SW": 1})
    with factory() as session:
        reserve_cart(session, owner, clock=clock, ttl=TTL)
    other = new_cart(factory, FixedClock(NOW + TTL), {"SW": 1})  # its edit already swept it ...
    statuses_before = statuses(factory)
    assert statuses_before == ["expired"]  # ... so re-create an overdue hold for the race
    with factory() as session:
        reserve_cart(session, owner, clock=clock, ttl=TTL)  # active again, due at NOW + TTL
    late = FixedClock(NOW + TTL)
    reservation_id = max(statuses_by_id(factory))

    hold = blocker(engine, "SELECT id FROM reservations WHERE id = :id FOR NO KEY UPDATE", id=reservation_id)
    swept = race.start(lambda s: reserve_cart(s, other, clock=late, ttl=TTL))  # sweeps it first
    edited = race.start(lambda s: remove_line(s, owner, "SW", clock=late))  # cart lock, then the reservation
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    no_errors(race.outcomes())
    assert swept.result().created
    ended_by_edit = edited.result().ended_reservation
    assert statuses(factory)[:2] == ["expired", "expired"]  # the owner's reservation ended once, as expired
    assert ended_by_edit is None or ended_by_edit.status == "expired"
    assert movement_reasons(factory).count("reservation_expiry") == 2  # one per reservation, never twice
    assert stock(factory, "SW") == (1, 1)
    check_invariants(factory)


# --- P11: the leftover reservation is ended before any hold, in one ascending variant pass ---------


def test_p11_leftover_release_and_new_holds_lock_variants_in_one_ascending_pass(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    # Regression for D1. The leftover reservation holds the HIGH variant; the new lines need the
    # LOW one. Releasing then holding in two passes would lock HIGH, then wait for LOW: a
    # deadlock against a transaction that locks LOW, then HIGH.
    setup(factory, lambda s: make_product(s, "p11", variants=[V(sku="LOW", size="S"), V(sku="HIGH")]))
    low, high = ids(factory, "LOW", "HIGH")
    assert low < high
    cart_id = new_cart(factory, clock, {"HIGH": 1})
    with factory() as session:
        reserve_cart(session, cart_id, clock=clock, ttl=TTL)
    # A leftover: the cart changes behind the services' back (no release), so the active
    # reservation no longer matches the cart version.
    with factory() as session, unit_of_work(session):
        session.execute(update(Cart).where(Cart.id == cart_id).values(version=Cart.version + 1))
        session.execute(update(CartItem).where(CartItem.cart_id == cart_id).values(variant_id=low))

    hold = blocker(engine, "SELECT id FROM variants WHERE id = :id FOR NO KEY UPDATE", id=low)
    try:
        reserving = race.start(lambda s: reserve_cart(s, cart_id, clock=clock, ttl=TTL))
        wait_for_lock_waiters(engine, 1, race.futures)  # it waits for LOW ...
        # ... without holding HIGH: the leftover's variant was not locked before the lower one.
        # NOWAIT fails immediately (LockNotAvailable) if HIGH is already locked: bounded time.
        hold.execute(text("SELECT id FROM variants WHERE id = :id FOR NO KEY UPDATE NOWAIT"), {"id": high})
    finally:
        hold.rollback()  # always release, so a failure can't leave the reservation waiting
        hold.close()

    result = reserving.result(timeout=RESULT_TIMEOUT)
    assert result.created
    assert statuses(factory) == ["released", "active"]
    assert (stock(factory, "HIGH"), stock(factory, "LOW")) == ((10, 0), (10, 1))
    check_invariants(factory)


def test_p11_old_two_pass_order_would_have_deadlocked(engine: Engine, factory: sessionmaker[Session]) -> None:
    # Control for the test above: shows the hazard is real, with plain SQL in the old order.
    setup(factory, lambda s: make_product(s, "ctl", variants=[V(sku="CL", size="S"), V(sku="CH")]))
    low, high = ids(factory, "CL", "CH")
    first = blocker(engine, "UPDATE variants SET updated_at = updated_at WHERE id = :id", id=high)
    second = blocker(engine, "UPDATE variants SET updated_at = updated_at WHERE id = :id", id=low)
    done = threading.Event()
    errors: list[BaseException] = []

    def first_then_low() -> None:
        try:
            first.execute(text("UPDATE variants SET updated_at = updated_at WHERE id = :id"), {"id": low})
        except BaseException as error:
            errors.append(error)
            first.rollback()  # a deadlock victim releases its locks at once, so the other side finishes
        finally:
            done.set()

    thread = threading.Thread(target=first_then_low)
    thread.start()
    wait_for_lock_waiters(engine, 1)
    try:
        second.execute(text("UPDATE variants SET updated_at = updated_at WHERE id = :id"), {"id": high})
    except OperationalError as error:
        errors.append(error)
        second.rollback()  # likewise if PostgreSQL picks this side as the victim
    # Bounded: deadlock detection takes deadlock_timeout (1 s by default); lock_timeout (10 s)
    # caps the wait if no deadlock is detected, and then the assertion below fails.
    done.wait(RESULT_TIMEOUT)
    thread.join(RESULT_TIMEOUT)
    for connection in (first, second):
        connection.rollback()
        connection.close()
    assert any("deadlock detected" in str(error) for error in errors), errors
