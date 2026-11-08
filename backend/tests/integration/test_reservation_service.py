"""Reservation service (ADR-024, ARCHITECTURE §3.1), driven by a FixedClock.

Runs on SQLite locally and on PostgreSQL in CI. Real-concurrency tests (P1-P11) are a separate
checkpoint; the lost-race paths here are made deterministic by running a competing request at
the point where a concurrent one could interleave (between the read and the main transaction).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, TypeVar

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.db import create_db_engine, session_factory, unit_of_work
from app.domain.cart.models import CART_IDLE_LIFETIME, Cart, CartItem
from app.domain.catalog.models import Product, Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.reservations import service
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import (
    CartEmpty,
    CartHasUnavailableItems,
    CartNotFound,
    ReservationConflict,
    ReservationShortfall,
    active_reservation,
    expire_reservation,
    release_cart_reservation,
    reserve_cart,
    sweep_overdue,
)
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.reservation_builders import NOW, TTL, new_cart, stock, variant_ids

T = TypeVar("T")


# --- fixtures and helpers --------------------------------------------------------------------


@pytest.fixture
def factory(migrated_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(migrated_database_url)
    factory = session_factory(engine)
    with factory() as session:
        make_product(
            session,
            "shell",
            variants=[
                V(sku="A-S", size="S", on_hand=5),
                V(sku="A-M", size="M", on_hand=2),
                V(sku="A-L", size="L", on_hand=0),
                V(sku="A-XL", size="XL", on_hand=4, status="archived"),
            ],
        )
        make_product(session, "retired", status="archived", variants=[V(sku="B-M", on_hand=3)])
        make_product(session, "single", variants=[V(sku="C-M", on_hand=1)])
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def s(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def look(session: Session, read: Callable[[], T]) -> T:
    """Read, then close the read transaction so the next service call can start its own."""
    try:
        return read()
    finally:
        session.rollback()


def cart_with(session: Session, seed: str, lines: dict[str, int], *, expires_at: datetime | None = None) -> int:
    with unit_of_work(session):
        cart = new_cart(session, seed)
        if expires_at is not None:
            cart.updated_at = expires_at - CART_IDLE_LIFETIME
            cart.expires_at = expires_at
        for variant_id, quantity in zip(variant_ids(session, *lines), lines.values(), strict=True):
            session.add(
                CartItem(cart_id=cart.id, variant_id=variant_id, quantity=quantity, created_at=NOW, updated_at=NOW)
            )
        session.flush()
        return cart.id


def cart_row(session: Session, cart_id: int) -> tuple[int, datetime]:
    row = look(session, lambda: session.execute(select(Cart.version, Cart.expires_at).where(Cart.id == cart_id)).one())
    return row.version, row.expires_at


def statuses(session: Session) -> dict[str, str]:
    return look(session, lambda: dict(session.execute(select(Reservation.public_id, Reservation.status)).all()))


def reasons(session: Session, public_id: str) -> list[tuple[str, str, int]]:
    rows = look(
        session,
        lambda: session.execute(
            select(Variant.sku, InventoryMovement.reason, InventoryMovement.delta_reserved)
            .join(Variant, Variant.id == InventoryMovement.variant_id)
            .join(Reservation, Reservation.id == InventoryMovement.reservation_id)
            .where(Reservation.public_id == public_id)
            .order_by(InventoryMovement.id)
        ).all(),
    )
    return [(sku, reason, delta) for sku, reason, delta in rows]


def reserved(session: Session, sku: str) -> int:
    return look(session, lambda: stock(session, sku)[1])


def movements(session: Session) -> int:
    return look(session, lambda: len(session.execute(select(InventoryMovement.id)).all()))


def check(session: Session) -> None:
    look(session, lambda: assert_inventory_invariants(session))


def write(session: Session, *statements: Any) -> None:
    with unit_of_work(session):
        for statement in statements:
            session.execute(statement)


def reserve(session: Session, cart_id: int, clock: FixedClock, **kwargs: Any) -> service.ReserveResult:
    return reserve_cart(session, cart_id, clock=clock, ttl=TTL, **kwargs)


# --- reserve_cart: success and idempotency ---------------------------------------------------


def test_reserve_holds_every_line(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-M": 1, "A-S": 2}, expires_at=NOW + timedelta(days=3))
    result = reserve(s, cart_id, clock)

    assert result.created
    view = result.reservation
    assert view.status == "active"
    assert (view.created_at, view.expires_at, view.ended_at) == (NOW, NOW + TTL, None)
    assert [(item.sku, item.quantity) for item in view.items] == [("A-S", 2), ("A-M", 1)]  # ascending variant id
    assert len(view.public_id) == 32
    assert cart_row(s, cart_id) == (1, NOW + CART_IDLE_LIFETIME)  # version CAS; 30-day expiry refreshed (C)
    assert view.cart_version == 1
    assert (reserved(s, "A-S"), reserved(s, "A-M")) == (2, 1)
    assert reasons(s, view.public_id) == [("A-S", "reservation_hold", 2), ("A-M", "reservation_hold", 1)]
    assert not s.in_transaction()
    check(s)


def test_repeated_reservation_returns_the_same_one(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    first = reserve(s, cart_id, clock)
    clock.advance(TTL - timedelta(microseconds=1))  # still unexpired
    again = reserve(s, cart_id, clock)

    assert (again.created, again.reservation) == (False, first.reservation)
    assert cart_row(s, cart_id)[0] == 1  # nothing written
    assert reserved(s, "A-S") == 2
    assert movements(s) == 1
    check(s)


def test_version_mismatch_releases_the_leftover_before_holding(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    first = reserve(s, cart_id, clock).reservation
    # Simulate a cart change that bypassed the edit rules: the active reservation no longer
    # matches the cart version.
    write(s, update(Cart).where(Cart.id == cart_id).values(version=Cart.version + 1))

    clock.advance(timedelta(minutes=1))
    second = reserve(s, cart_id, clock)

    assert second.created
    assert second.reservation.public_id != first.public_id
    assert second.reservation.cart_version == 3
    assert statuses(s) == {first.public_id: "released", second.reservation.public_id: "active"}
    assert reasons(s, first.public_id) == [("A-S", "reservation_hold", 2), ("A-S", "reservation_release", -2)]
    assert reserved(s, "A-S") == 2  # not 4: the leftover was released first
    # The leftover's release movement precedes the new hold.
    order = look(
        s,
        lambda: [
            reason for (reason,) in s.execute(select(InventoryMovement.reason).order_by(InventoryMovement.id)).all()
        ],
    )
    assert order == ["reservation_hold", "reservation_release", "reservation_hold"]
    check(s)


def test_leftover_on_other_variants_is_released_and_new_lines_held(s: Session, clock: FixedClock) -> None:
    # D1 path (functional check; the lock-order check is PostgreSQL-only, P11): the leftover
    # holds A-M (higher id) while the new lines need A-S (lower id). Both variants are locked in
    # one ascending pass, then the leftover is released and the new lines held.
    cart_id = cart_with(s, "c1", {"A-M": 1})
    leftover = reserve(s, cart_id, clock).reservation
    write(
        s,
        update(Cart).where(Cart.id == cart_id).values(version=Cart.version + 1),
        update(CartItem).where(CartItem.cart_id == cart_id).values(variant_id=variant_ids_of(s, "A-S")[0]),
    )
    result = reserve(s, cart_id, clock)
    assert result.created
    assert [(item.sku, item.quantity) for item in result.reservation.items] == [("A-S", 1)]
    assert statuses(s) == {leftover.public_id: "released", result.reservation.public_id: "active"}
    assert (reserved(s, "A-M"), reserved(s, "A-S")) == (0, 1)
    check(s)


def test_expiry_boundary_is_exact(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 1})
    first = reserve(s, cart_id, clock).reservation
    clock.set(NOW + TTL - timedelta(microseconds=1))
    assert reserve(s, cart_id, clock).reservation == first  # one microsecond before: still active

    clock.set(NOW + TTL)  # expires_at <= now: overdue
    second = reserve(s, cart_id, clock)
    assert second.created
    assert statuses(s)[first.public_id] == "expired"
    assert reasons(s, first.public_id)[-1] == ("A-S", "reservation_expiry", -1)
    ended_at = look(
        s, lambda: s.execute(select(Reservation.ended_at).where(Reservation.public_id == first.public_id)).scalar_one()
    )
    assert ended_at == NOW + TTL
    assert second.reservation.expires_at == NOW + 2 * TTL
    assert reserved(s, "A-S") == 1
    check(s)


# --- reserve_cart: rejections roll back everything -------------------------------------------


def test_unknown_and_expired_carts_are_not_found(s: Session, clock: FixedClock) -> None:
    with pytest.raises(CartNotFound):
        reserve(s, 999, clock)
    cart_id = cart_with(s, "c1", {"A-S": 1}, expires_at=NOW)
    with pytest.raises(CartNotFound):
        reserve(s, cart_id, clock)
    assert cart_row(s, cart_id)[0] == 0
    assert not s.in_transaction()


def test_empty_cart_is_rejected_without_changes(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {})
    expires_at = cart_row(s, cart_id)[1]
    with pytest.raises(CartEmpty):
        reserve(s, cart_id, clock)
    assert cart_row(s, cart_id) == (0, expires_at)
    assert statuses(s) == {}


@pytest.mark.parametrize("sku", ["A-XL", "B-M"], ids=["archived-variant", "archived-product"])
def test_unavailable_lines_block_the_reservation(s: Session, clock: FixedClock, sku: str) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 1, sku: 1})
    with pytest.raises(CartHasUnavailableItems) as raised:
        reserve(s, cart_id, clock)
    assert raised.value.details == [{"sku": sku}]
    assert cart_row(s, cart_id)[0] == 0
    assert (statuses(s), movements(s), reserved(s, "A-S")) == ({}, 0, 0)


def test_shortfall_rolls_back_everything_including_the_leftover_release(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 1})
    leftover = reserve(s, cart_id, clock).reservation
    # Make the leftover stale, then ask for more than is available.
    write(
        s,
        update(Cart).where(Cart.id == cart_id).values(version=Cart.version + 1),
        update(CartItem).where(CartItem.cart_id == cart_id).values(quantity=2),
    )
    with unit_of_work(s):
        for vid, qty in zip(variant_ids(s, "A-M", "A-L"), (3, 1), strict=True):
            s.add(CartItem(cart_id=cart_id, variant_id=vid, quantity=qty, created_at=NOW, updated_at=NOW))
    before = cart_row(s, cart_id)

    with pytest.raises(ReservationShortfall) as raised:
        reserve(s, cart_id, clock)

    # Every short line by SKU; D-5: no counts anywhere.
    assert raised.value.details == [{"sku": "A-M"}, {"sku": "A-L"}]
    assert raised.value.message == "Some items are not available in the requested quantity."
    assert cart_row(s, cart_id) == before  # version and expiry rolled back
    assert statuses(s) == {leftover.public_id: "active"}  # the leftover's release rolled back too
    assert (reserved(s, "A-S"), reserved(s, "A-M"), movements(s)) == (1, 0, 1)
    assert not s.in_transaction()
    check(s)


def test_ttl_must_be_positive(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 1})
    with pytest.raises(ValueError, match="ttl"):
        reserve_cart(s, cart_id, clock=clock, ttl=timedelta(0))


# --- reserve_cart: lost races (made deterministic) -------------------------------------------


def _interleave(monkeypatch: pytest.MonkeyPatch, competitor: Callable[[], None]) -> None:
    """Run ``competitor`` once, between reserve_cart's read and its main transaction."""
    real_sweep = service.sweep_overdue
    pending = [competitor]

    def sweep_then_compete(*args: Any, **kwargs: Any) -> int:
        swept = real_sweep(*args, **kwargs)
        if pending:
            pending.pop()()
        return swept

    monkeypatch.setattr(service, "sweep_overdue", sweep_then_compete)


def test_lost_race_returns_the_winners_reservation(
    s: Session, factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    winner: list[service.ReserveResult] = []

    def competing_reserve() -> None:
        with factory() as other:
            winner.append(reserve(other, cart_id, clock))

    _interleave(monkeypatch, competing_reserve)
    result = reserve(s, cart_id, clock)

    assert winner[0].created
    assert (result.created, result.reservation) == (False, winner[0].reservation)
    assert reserved(s, "A-S") == 2
    check(s)


def test_lost_race_to_a_cart_change_is_a_conflict(
    s: Session, factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})

    def competing_edit() -> None:
        with factory() as other:
            write(other, update(Cart).where(Cart.id == cart_id).values(version=Cart.version + 1))

    _interleave(monkeypatch, competing_edit)
    with pytest.raises(ReservationConflict):
        reserve(s, cart_id, clock)
    assert cart_row(s, cart_id)[0] == 1  # only the competitor's change
    assert (statuses(s), movements(s)) == ({}, 0)


# --- release_cart_reservation ------------------------------------------------------------------


def test_customer_release(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2, "A-M": 1})
    reservation = reserve(s, cart_id, clock).reservation
    clock.advance(timedelta(minutes=5))

    released = release_cart_reservation(s, cart_id, clock=clock)

    assert released is not None
    assert (released.public_id, released.status, released.ended_at) == (
        reservation.public_id,
        "released",
        NOW + timedelta(minutes=5),
    )
    assert cart_row(s, cart_id)[0] == 2  # the lock advanced the version (decision B)
    assert (reserved(s, "A-S"), reserved(s, "A-M")) == (0, 0)
    assert reasons(s, reservation.public_id)[2:] == [
        ("A-S", "reservation_release", -2),
        ("A-M", "reservation_release", -1),
    ]
    check(s)


def test_releasing_an_overdue_reservation_records_expiry(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    reservation = reserve(s, cart_id, clock).reservation
    clock.set(NOW + TTL)

    released = release_cart_reservation(s, cart_id, clock=clock)

    assert released is not None
    assert released.status == "expired"
    assert reasons(s, reservation.public_id)[-1] == ("A-S", "reservation_expiry", -2)
    check(s)


def test_repeated_release_never_releases_twice(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    reserve(s, cart_id, clock)
    assert release_cart_reservation(s, cart_id, clock=clock) is not None
    assert release_cart_reservation(s, cart_id, clock=clock) is None
    assert release_cart_reservation(s, cart_id, clock=clock) is None
    assert reserved(s, "A-S") == 0
    assert movements(s) == 2  # one hold, one release
    assert cart_row(s, cart_id)[0] == 4  # each release takes the lock (decision B)
    check(s)


def test_release_with_nothing_to_release(s: Session, clock: FixedClock) -> None:
    assert release_cart_reservation(s, 999, clock=clock) is None
    cart_id = cart_with(s, "c1", {"A-S": 1})
    assert release_cart_reservation(s, cart_id, clock=clock) is None
    assert movements(s) == 0


def test_release_after_archiving(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    reserve(s, cart_id, clock)
    write(
        s,
        update(Variant).where(Variant.sku == "A-S").values(status="archived"),
        update(Product).where(Product.slug == "shell").values(status="archived"),
    )
    released = release_cart_reservation(s, cart_id, clock=clock)
    assert released is not None
    assert released.status == "released"
    assert reserved(s, "A-S") == 0
    check(s)


def test_release_works_after_the_cart_itself_expired(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 1}, expires_at=NOW + timedelta(minutes=5))
    reserve(s, cart_id, clock)  # refreshes the cart expiry ...
    write(s, update(Cart).where(Cart.id == cart_id).values(expires_at=NOW + timedelta(minutes=1)))
    clock.set(NOW + timedelta(minutes=2))  # ... but suppose it lapsed anyway
    released = release_cart_reservation(s, cart_id, clock=clock)
    assert released is not None
    assert reserved(s, "A-S") == 0
    check(s)


# --- expiry, active_reservation and sweeps ---------------------------------------------------


def test_expire_reservation_only_when_due_and_only_once(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    reserve(s, cart_id, clock)
    reservation_id = look(s, lambda: s.execute(select(Reservation.id)).scalar_one())

    assert not expire_reservation(s, reservation_id, clock=clock)  # not due
    clock.set(NOW + TTL)
    assert expire_reservation(s, reservation_id, clock=clock)
    assert not expire_reservation(s, reservation_id, clock=clock)  # already ended
    assert reserved(s, "A-S") == 0
    assert movements(s) == 2
    assert cart_row(s, cart_id)[0] == 1  # expiry never touches the cart
    check(s)


def test_active_reservation_lazily_expires(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, "c1", {"A-S": 2})
    reservation = reserve(s, cart_id, clock).reservation
    assert active_reservation(s, cart_id, clock=clock) == reservation
    assert active_reservation(s, 999, clock=clock) is None

    clock.set(NOW + TTL)
    assert active_reservation(s, cart_id, clock=clock) is None  # this read wrote the expiry
    assert statuses(s) == {reservation.public_id: "expired"}
    assert reserved(s, "A-S") == 0
    assert not s.in_transaction()
    check(s)


def _overdue_reservations(s: Session, clock: FixedClock, plans: list[tuple[str, dict[str, int], int]]) -> list[int]:
    """Carts reserved at staggered times (minutes after NOW), then the clock moved past all TTLs."""
    cart_ids = []
    for seed, lines, minute in plans:
        cart_id = cart_with(s, seed, lines)
        clock.set(NOW + timedelta(minutes=minute))
        reserve(s, cart_id, clock)
        cart_ids.append(cart_id)
    clock.set(NOW + timedelta(minutes=max(minute for _s, _l, minute in plans)) + TTL)
    return cart_ids


def test_sweep_expires_overdue_reservations_for_the_variants(s: Session, clock: FixedClock) -> None:
    a, b, c, d = _overdue_reservations(
        s, clock, [("a", {"A-S": 1}, 0), ("b", {"A-S": 1, "A-M": 1}, 1), ("c", {"A-M": 1}, 2), ("d", {"A-S": 1}, 3)]
    )
    write(s, update(Reservation).where(Reservation.cart_id == d).values(expires_at=clock.now() + TTL))  # not due
    versions = [cart_row(s, cart_id)[0] for cart_id in (a, b, c, d)]

    assert sweep_overdue(s, variant_ids=variant_ids_of(s, "A-S"), exclude_cart_id=None, clock=clock) == 2
    assert _status_by_cart(s) == {a: "expired", b: "expired", c: "active", d: "active"}
    assert [cart_row(s, cart_id)[0] for cart_id in (a, b, c, d)] == versions  # sweeps never touch carts
    check(s)


def test_sweep_respects_limit_order_and_exclusion(s: Session, clock: FixedClock) -> None:
    a, b, c = _overdue_reservations(s, clock, [("a", {"A-S": 1}, 2), ("b", {"A-S": 1}, 0), ("c", {"A-S": 1}, 1)])
    ids = variant_ids_of(s, "A-S")

    assert sweep_overdue(s, variant_ids=ids, exclude_cart_id=b, clock=clock, limit=1) == 1
    assert _status_by_cart(s) == {a: "active", b: "active", c: "expired"}  # oldest non-excluded first
    assert sweep_overdue(s, variant_ids=ids, exclude_cart_id=None, clock=clock, limit=0) == 0
    assert sweep_overdue(s, variant_ids=[], exclude_cart_id=None, clock=clock) == 0
    assert sweep_overdue(s, variant_ids=ids, exclude_cart_id=None, clock=clock) == 2
    check(s)


def test_reserving_sweeps_other_carts_overdue_holds(s: Session, clock: FixedClock) -> None:
    other = cart_with(s, "other", {"C-M": 1})
    reserve(s, other, clock)  # holds the only unit
    mine = cart_with(s, "mine", {"C-M": 1})
    with pytest.raises(ReservationShortfall):
        reserve(s, mine, clock)  # not overdue yet: genuinely unavailable

    clock.set(NOW + TTL)
    assert reserve(s, mine, clock).created
    assert _status_by_cart(s) == {other: "expired", mine: "active"}
    assert reserved(s, "C-M") == 1
    check(s)


def variant_ids_of(session: Session, *skus: str) -> list[int]:
    return look(session, lambda: variant_ids(session, *skus))


def _status_by_cart(session: Session) -> dict[int, str]:
    rows = look(
        session,
        lambda: session.execute(select(Reservation.cart_id, Reservation.status).order_by(Reservation.id)).all(),
    )
    latest: dict[int, str] = {}
    for cart_id, status in rows:
        latest[cart_id] = status
    return latest


# --- errors ------------------------------------------------------------------------------


def test_error_codes_and_statuses() -> None:
    assert (CartNotFound.status_code, CartNotFound.code) == (404, "cart_not_found")
    assert (CartEmpty.status_code, CartEmpty.code) == (409, "cart_empty")
    assert (CartHasUnavailableItems.status_code, CartHasUnavailableItems.code) == (409, "cart_has_unavailable_items")
    assert (ReservationShortfall.status_code, ReservationShortfall.code) == (409, "insufficient_available_stock")
    assert (ReservationConflict.status_code, ReservationConflict.code) == (409, "reservation_conflict")
