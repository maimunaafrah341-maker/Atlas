"""Deterministic PostgreSQL concurrency tests for orders (M3.1, ADR-025).

Same method as test_reservation_concurrency.py (whose harness this reuses): a blocker connection
holds the row lock the operations will contend for, the operations start in threads, the test
waits until the expected number of backends are blocked on a lock (pg_stat_activity, not a
sleep), then releases the blocker. Every connection has lock and statement timeouts, and
PostgreSQL's deadlock detector turns a deadlock into an error the test sees.
"""

# The harness's fixtures (engine, factory, race, clock) are imported so pytest finds them here;
# test parameters with the same names are how fixtures are requested, not redefinitions.
# ruff: noqa: F811

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.domain.cart.models import CartItem
from app.domain.cart.service import set_quantity
from app.domain.catalog.models import Variant
from app.domain.orders.holds import CheckoutInProgress, expire_order_if_due
from app.domain.orders.models import Order
from app.domain.orders.service import CheckoutConflict, ReservationRequired, cancel_order, create_order
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import expire_reservation, reserve_cart
from tests.conftest import TEST_DATABASE_URL
from tests.factories import V, make_product
from tests.integration.test_reservation_concurrency import (  # noqa: F401  (fixtures: engine, factory, race, clock)
    RESULT_TIMEOUT,
    Race,
    blocker,
    check_invariants,
    clock,
    engine,
    factory,
    movement_reasons,
    new_cart,
    race,
    setup,
    stock,
    wait_for_lock_waiters,
)
from tests.order_support import HOLD, add_shipping
from tests.reservation_builders import NOW, TTL

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="real concurrency needs PostgreSQL (TEST_DATABASE_URL)"),
]


def shop(session: Session) -> None:
    make_product(session, "o", variants=[V(sku="O-S", size="S", on_hand=5), V(sku="O-M", on_hand=5)])
    add_shipping(session)


def reserved_cart(factory: sessionmaker[Session], clock: FixedClock, lines: dict[str, int]) -> int:
    cart_id = new_cart(factory, clock, lines)
    with factory() as session:
        reserve_cart(session, cart_id, clock=clock, ttl=TTL)
    return cart_id


def create(cart_id: int, clock: FixedClock) -> Any:
    return lambda s: create_order(s, cart_id, ship_country="FR", lang="en", clock=clock, hold=HOLD)


def orders(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    with factory() as session:
        rows = session.execute(
            select(Order.status, Reservation.status)
            .join(Reservation, Reservation.id == Order.reservation_id)
            .order_by(Order.id)
        ).all()
    return [(order, reservation) for order, reservation in rows]


def order_id_and_ref(factory: sessionmaker[Session]) -> tuple[int, str]:
    with factory() as session:
        row = session.execute(select(Order.id, Order.public_ref)).one()
    return row.id, row.public_ref


def errors(outcomes: list[Any], *expected: type[BaseException]) -> list[BaseException]:
    unexpected = [o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, expected)]
    assert unexpected == [], unexpected
    return [o for o in outcomes if isinstance(o, BaseException)]


# --- O1: parallel create requests for one cart (client retries) ----------------------------------


def test_o1_parallel_creates_for_one_cart_make_one_order(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    cart_id = reserved_cart(factory, clock, {"O-S": 2})

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    for _ in range(4):
        race.start(create(cart_id, clock))
    wait_for_lock_waiters(engine, 4, race.futures)  # all four at the cart-lock compare-and-set
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    errors(outcomes)  # none: the losers recover the winner's order
    assert sum(o.created for o in outcomes) == 1
    assert len({o.order.public_ref for o in outcomes}) == 1
    assert orders(factory) == [("pending_approval", "order_hold")]
    assert stock(factory, "O-S") == (5, 2)
    assert movement_reasons(factory) == ["reservation_hold"]  # adopted, never held again
    check_invariants(factory)


# --- O2: create racing a cart edit ------------------------------------------------------------


def test_o2_create_and_cart_edit_serialise_on_the_cart(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    cart_id = reserved_cart(factory, clock, {"O-S": 2})

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    created = race.start(create(cart_id, clock))
    edited = race.start(lambda s: set_quantity(s, cart_id, "O-S", 3, clock=clock))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    failed = errors(outcomes, CheckoutInProgress, CheckoutConflict, ReservationRequired)
    assert len(failed) == 1  # exactly one of them won
    with factory() as session:
        quantity = session.execute(select(CartItem.quantity).where(CartItem.cart_id == cart_id)).scalar_one()
    if isinstance(edited.exception(timeout=RESULT_TIMEOUT), CheckoutInProgress):  # the order came first
        assert created.result().created
        assert orders(factory) == [("pending_approval", "order_hold")]
        assert (quantity, stock(factory, "O-S")) == (2, (5, 2))
    else:  # the edit came first: it released the reservation, so there is nothing to adopt
        assert isinstance(created.exception(timeout=RESULT_TIMEOUT), CheckoutConflict | ReservationRequired)
        assert orders(factory) == []
        assert (quantity, stock(factory, "O-S")) == (3, (5, 0))
    check_invariants(factory)


# --- O3: create racing the cart-side expiry of the reservation it adopts -------------------------


def test_o3_create_and_reservation_expiry_end_it_exactly_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    cart_id = reserved_cart(factory, clock, {"O-M": 1})
    with factory() as session:
        reservation_id = session.execute(select(Reservation.id)).scalar_one()
    just_in_time = FixedClock(NOW + TTL - timedelta(seconds=1))

    hold = blocker(engine, "SELECT id FROM reservations WHERE id = :id FOR NO KEY UPDATE", id=reservation_id)
    race.start(create(cart_id, just_in_time))
    expired = race.start(lambda s: expire_reservation(s, reservation_id, clock=FixedClock(NOW + TTL)))
    wait_for_lock_waiters(engine, 2, race.futures)  # both at the reservation compare-and-set
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    errors(outcomes, ReservationRequired)
    adopted = not isinstance(outcomes[0], BaseException)
    assert adopted != expired.result()  # exactly one of them moved it out of 'active'
    if adopted:
        assert orders(factory) == [("pending_approval", "order_hold")]
        assert stock(factory, "O-M") == (5, 1)
        assert movement_reasons(factory) == ["reservation_hold"]
    else:  # the failed create rolled back its order and cart lock
        assert orders(factory) == []
        assert stock(factory, "O-M") == (5, 0)
        assert movement_reasons(factory) == ["reservation_hold", "reservation_expiry"]
    check_invariants(factory)


# --- O4: cancel racing lazy expiry of an overdue order ---------------------------------------------


def test_o4_cancel_and_expiry_release_the_hold_exactly_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    cart_id = reserved_cart(factory, clock, {"O-S": 1, "O-M": 2})
    with factory() as session:
        create(cart_id, clock)(session)
    order_id, ref = order_id_and_ref(factory)
    late = FixedClock(NOW + HOLD)  # due for both

    hold = blocker(engine, "SELECT id FROM orders WHERE id = :id FOR NO KEY UPDATE", id=order_id)
    cancelled = race.start(lambda s: cancel_order(s, cart_id, ref, clock=late))
    expired = race.start(lambda s: expire_order_if_due(s, order_id, clock=late))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    errors(race.outcomes())
    assert cancelled.result().status == "expired"  # overdue: both paths record expiry
    assert orders(factory) == [("expired", "expired")]
    assert movement_reasons(factory) == ["reservation_hold"] * 2 + ["reservation_expiry"] * 2  # once per item
    assert (stock(factory, "O-S"), stock(factory, "O-M")) == ((5, 0), (5, 0))
    assert isinstance(expired.result(), bool)
    check_invariants(factory)


# --- O5: repeated cancel requests ------------------------------------------------------------------


def test_o5_parallel_cancels_release_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    cart_id = reserved_cart(factory, clock, {"O-S": 3})
    with factory() as session:
        create(cart_id, clock)(session)
    order_id, ref = order_id_and_ref(factory)

    hold = blocker(engine, "SELECT id FROM orders WHERE id = :id FOR NO KEY UPDATE", id=order_id)
    for _ in range(3):
        race.start(lambda s: cancel_order(s, cart_id, ref, clock=clock))
    wait_for_lock_waiters(engine, 3, race.futures)
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    errors(outcomes)
    assert {o.status for o in outcomes} == {"cancelled"}
    assert orders(factory) == [("cancelled", "released")]
    assert movement_reasons(factory) == ["reservation_hold", "reservation_release"]
    assert stock(factory, "O-S") == (5, 0)
    with factory() as session:  # the bag is editable again
        set_quantity(session, cart_id, "O-S", 1, clock=clock)
        assert session.execute(select(Variant.stock_reserved).where(Variant.sku == "O-S")).scalar_one() == 0
    check_invariants(factory)
