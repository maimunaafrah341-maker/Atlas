"""Cart write service (ADR-024, ARCHITECTURE §3.1 rules 1, 6, 8, 11), driven by a FixedClock.

Runs on SQLite locally and on PostgreSQL in CI (not PostgreSQL-only).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, TypeVar

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.db import create_db_engine, session_factory, unit_of_work
from app.domain.cart.models import CART_IDLE_LIFETIME, MAX_CART_LINES, Cart, CartItem
from app.domain.cart.service import (
    CartLineLimit,
    find_cart,
    new_cart_token,
    remove_line,
    set_quantity,
)
from app.domain.catalog.models import Product, Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.inventory.service import InsufficientAvailableStock, InvalidQuantity, VariantInactive, VariantNotFound
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import CartNotFound, reserve_cart
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.reservation_builders import NOW, TTL, stock

T = TypeVar("T")


@pytest.fixture
def factory(migrated_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(migrated_database_url)
    factory = session_factory(engine)
    with factory() as session:
        make_product(
            session,
            "shell",
            variants=[
                V(sku="A-S", size="S", on_hand=12),
                V(sku="A-M", size="M", on_hand=2),
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
    try:
        return read()
    finally:
        session.rollback()


def write(session: Session, *statements: Any) -> None:
    with unit_of_work(session):
        for statement in statements:
            session.execute(statement)


def cart_row(session: Session, cart_id: int) -> tuple[int, datetime, datetime]:
    row = look(
        session,
        lambda: session.execute(select(Cart.version, Cart.updated_at, Cart.expires_at).where(Cart.id == cart_id)).one(),
    )
    return row.version, row.updated_at, row.expires_at


def lines(session: Session, cart_id: int) -> dict[str, int]:
    rows = look(
        session,
        lambda: session.execute(
            select(Variant.sku, CartItem.quantity)
            .join(Variant, Variant.id == CartItem.variant_id)
            .where(CartItem.cart_id == cart_id)
        ).all(),
    )
    return dict(rows)  # type: ignore[arg-type]


def count(session: Session, model: Any) -> int:
    return look(session, lambda: session.execute(select(func.count()).select_from(model)).scalar_one())


def reserved(session: Session, sku: str) -> int:
    return look(session, lambda: stock(session, sku)[1])


def reservation_statuses(session: Session) -> list[str]:
    return look(session, lambda: list(session.execute(select(Reservation.status).order_by(Reservation.id)).scalars()))


def check(session: Session) -> None:
    look(session, lambda: assert_inventory_invariants(session))


def new_cart(session: Session, clock: FixedClock, sku: str = "A-S", quantity: int = 1) -> int:
    return set_quantity(session, None, sku, quantity, clock=clock).cart.cart_id


# --- tokens and lookup -------------------------------------------------------------------------


def test_tokens_are_random_and_only_their_digest_is_stored(s: Session, clock: FixedClock) -> None:
    token, digest = new_cart_token()
    assert len(token) >= 43
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert new_cart_token()[0] != token

    change = set_quantity(s, None, "A-S", 2, clock=clock)
    assert change.new_token is not None
    assert change.new_token not in repr(change)  # never in reprs, so never in logs
    stored = look(s, lambda: s.execute(select(Cart.token_hash)).scalar_one())
    assert stored == hashlib.sha256(change.new_token.encode()).hexdigest()
    assert stored != change.new_token
    assert find_cart(s, change.new_token, clock=clock) == change.cart.cart_id


def test_unknown_malformed_and_expired_tokens_find_nothing(s: Session, clock: FixedClock) -> None:
    change = set_quantity(s, None, "A-S", 1, clock=clock)
    token = change.new_token
    assert token is not None
    assert find_cart(s, "not-a-real-token", clock=clock) is None
    assert find_cart(s, "", clock=clock) is None
    assert find_cart(s, "x" * 10_000, clock=clock) is None
    assert find_cart(s, hashlib.sha256(token.encode()).hexdigest(), clock=clock) is None  # the digest is not a token

    clock.set(NOW + CART_IDLE_LIFETIME - timedelta(microseconds=1))
    assert find_cart(s, token, clock=clock) == change.cart.cart_id
    clock.set(NOW + CART_IDLE_LIFETIME)  # expires_at <= now: expired
    assert find_cart(s, token, clock=clock) is None


def test_lookup_never_extends_expiry(s: Session, clock: FixedClock) -> None:
    change = set_quantity(s, None, "A-S", 1, clock=clock)
    assert change.new_token is not None
    before = cart_row(s, change.cart.cart_id)
    clock.advance(timedelta(days=10))
    assert find_cart(s, change.new_token, clock=clock) == change.cart.cart_id
    assert cart_row(s, change.cart.cart_id) == before
    assert not s.in_transaction()


# --- creating a cart ---------------------------------------------------------------------------


def test_new_cart_and_first_line_are_created_together(s: Session, clock: FixedClock) -> None:
    change = set_quantity(s, None, "A-S", 2, clock=clock)
    view = change.cart
    assert (view.version, view.expires_at) == (1, NOW + CART_IDLE_LIFETIME)
    assert [(line.sku, line.product_slug, line.size, line.quantity, line.available) for line in view.lines] == [
        ("A-S", "shell", "S", 2, True)
    ]
    assert (view.lines[0].unit_price_minor, view.lines[0].currency) == (5000, "USD")
    assert change.ended_reservation is None
    created, updated = look(s, lambda: s.execute(select(Cart.created_at, CartItem.updated_at).join(CartItem)).one())
    assert (created, updated) == (NOW, NOW)
    assert (count(s, Reservation), reserved(s, "A-S"), count(s, InventoryMovement)) == (0, 0, 0)


@pytest.mark.parametrize(
    ("sku", "quantity", "error"),
    [
        ("NOPE", 1, VariantNotFound),
        ("A-XL", 1, VariantInactive),
        ("B-M", 1, VariantInactive),
        ("A-M", 3, InsufficientAvailableStock),
        ("A-S", 11, InvalidQuantity),
    ],
    ids=["unknown", "archived-variant", "archived-product", "unavailable", "too-many"],
)
def test_failed_first_line_leaves_no_orphan_cart(
    s: Session, clock: FixedClock, sku: str, quantity: int, error: type[Exception]
) -> None:
    with pytest.raises(error):
        set_quantity(s, None, sku, quantity, clock=clock)
    assert (count(s, Cart), count(s, CartItem)) == (0, 0)
    assert not s.in_transaction()


# --- editing -----------------------------------------------------------------------------------


def test_setting_a_quantity_replaces_it(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    clock.advance(timedelta(days=2))
    change = set_quantity(s, cart_id, "A-S", 3, clock=clock)
    assert lines(s, cart_id) == {"A-S": 3}
    assert change.new_token is None
    version, updated_at, expires_at = cart_row(s, cart_id)
    assert (version, updated_at, expires_at) == (
        2,
        NOW + timedelta(days=2),
        NOW + timedelta(days=2) + CART_IDLE_LIFETIME,
    )
    created, line_updated = look(s, lambda: s.execute(select(CartItem.created_at, CartItem.updated_at)).one())
    assert (created, line_updated) == (NOW, NOW + timedelta(days=2))


@pytest.mark.parametrize("quantity", [0, -1, 11, True, 2.0, "2", None])
def test_invalid_quantities_change_nothing(s: Session, clock: FixedClock, quantity: Any) -> None:
    cart_id = new_cart(s, clock)
    with pytest.raises(InvalidQuantity) as raised:
        set_quantity(s, cart_id, "A-S", quantity, clock=clock)
    assert raised.value.status_code == 422
    assert cart_row(s, cart_id)[0] == 1


@pytest.mark.parametrize("quantity", [1, 10])
def test_quantity_boundaries_are_accepted(s: Session, clock: FixedClock, quantity: int) -> None:
    cart_id = new_cart(s, clock)
    set_quantity(s, cart_id, "A-S", quantity, clock=clock)
    assert lines(s, cart_id) == {"A-S": quantity}


def test_line_limit(s: Session, clock: FixedClock) -> None:
    with unit_of_work(s):
        for index in range(MAX_CART_LINES + 1):
            make_product(s, f"many-{index}", variants=[V(sku=f"MANY-{index}", on_hand=5)])
    cart_id = new_cart(s, clock, "MANY-0")
    for index in range(1, MAX_CART_LINES):
        set_quantity(s, cart_id, f"MANY-{index}", 1, clock=clock)
    version = cart_row(s, cart_id)[0]

    with pytest.raises(CartLineLimit) as raised:
        set_quantity(s, cart_id, f"MANY-{MAX_CART_LINES}", 1, clock=clock)
    assert raised.value.details == [{"limit": MAX_CART_LINES}]
    assert cart_row(s, cart_id)[0] == version
    set_quantity(s, cart_id, "MANY-0", 2, clock=clock)  # changing an existing line is still allowed
    assert len(lines(s, cart_id)) == MAX_CART_LINES


@pytest.mark.parametrize(
    ("sku", "error"), [("NOPE", VariantNotFound), ("A-XL", VariantInactive), ("B-M", VariantInactive)]
)
def test_unknown_or_archived_skus_are_rejected(s: Session, clock: FixedClock, sku: str, error: type[Exception]) -> None:
    cart_id = new_cart(s, clock)
    with pytest.raises(error):
        set_quantity(s, cart_id, sku, 1, clock=clock)
    assert cart_row(s, cart_id)[0] == 1
    assert lines(s, cart_id) == {"A-S": 1}


def test_unknown_or_expired_cart_ids_are_not_found(s: Session, clock: FixedClock) -> None:
    with pytest.raises(CartNotFound):
        set_quantity(s, 999, "A-S", 1, clock=clock)
    cart_id = new_cart(s, clock)
    clock.set(NOW + CART_IDLE_LIFETIME)
    with pytest.raises(CartNotFound):
        set_quantity(s, cart_id, "A-S", 2, clock=clock)
    with pytest.raises(CartNotFound):
        remove_line(s, cart_id, "A-S", clock=clock)
    assert cart_row(s, cart_id)[0] == 1


# --- reservations and availability --------------------------------------------------------------


def test_an_edit_releases_the_active_reservation_and_reserves_nothing(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    reservation = reserve_cart(s, cart_id, clock=clock, ttl=TTL).reservation
    clock.advance(timedelta(minutes=3))

    change = set_quantity(s, cart_id, "A-M", 1, clock=clock)

    assert change.ended_reservation is not None
    assert (change.ended_reservation.public_id, change.ended_reservation.status) == (reservation.public_id, "released")
    assert reservation_statuses(s) == ["released"]  # no new reservation
    assert (reserved(s, "A-S"), reserved(s, "A-M")) == (0, 0)
    assert lines(s, cart_id) == {"A-S": 2, "A-M": 1}
    check(s)


def test_an_edit_records_an_overdue_reservation_as_expired(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    reserve_cart(s, cart_id, clock=clock, ttl=TTL)
    clock.set(NOW + TTL)
    change = set_quantity(s, cart_id, "A-S", 1, clock=clock)
    assert change.ended_reservation is not None
    assert change.ended_reservation.status == "expired"
    check(s)


@pytest.mark.parametrize(
    ("sku", "quantity", "error"),
    [("NOPE", 1, VariantNotFound), ("B-M", 1, VariantInactive), ("A-M", 3, InsufficientAvailableStock)],
    ids=["unknown", "archived", "unavailable"],
)
def test_a_failed_edit_keeps_the_reservation(
    s: Session, clock: FixedClock, sku: str, quantity: int, error: type[Exception]
) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    reserve_cart(s, cart_id, clock=clock, ttl=TTL)
    before = cart_row(s, cart_id)
    clock.advance(timedelta(minutes=1))

    with pytest.raises(error):
        set_quantity(s, cart_id, sku, quantity, clock=clock)

    assert cart_row(s, cart_id) == before  # version and expiry rolled back
    assert reservation_statuses(s) == ["active"]  # the release rolled back too
    assert reserved(s, "A-S") == 2
    assert count(s, InventoryMovement) == 1
    check(s)


def test_availability_counts_the_carts_own_released_hold(s: Session, clock: FixedClock) -> None:
    mine = new_cart(s, clock, "A-M", 2)
    reserve_cart(s, mine, clock=clock, ttl=TTL)  # holds both units of A-M

    other = new_cart(s, clock, "A-S", 1)
    with pytest.raises(InsufficientAvailableStock) as raised:
        set_quantity(s, other, "A-M", 1, clock=clock)
    # D-5: the SKU and the customer's own requested quantity, never the available count.
    assert raised.value.details == [{"sku": "A-M", "requested": 1}]
    assert "0" not in raised.value.message

    # A genuine change: available only because the cart's own hold is released first.
    set_quantity(s, mine, "A-M", 1, clock=clock)
    assert (reserved(s, "A-M"), lines(s, mine)) == (0, {"A-M": 1})
    check(s)


def test_an_edit_sweeps_other_carts_overdue_holds_first(s: Session, clock: FixedClock) -> None:
    other = new_cart(s, clock, "C-M", 1)
    reserve_cart(s, other, clock=clock, ttl=TTL)
    clock.set(NOW + TTL)
    other_version = cart_row(s, other)[0]

    new_cart(s, clock, "C-M", 1)  # a new cart: no id to exclude

    assert reservation_statuses(s) == ["expired"]
    assert cart_row(s, other)[0] == other_version  # the sweep never touched the other cart
    check(s)


# --- removing ------------------------------------------------------------------------------------


def test_remove_releases_the_reservation_and_keeps_other_lines(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    set_quantity(s, cart_id, "A-M", 1, clock=clock)
    set_quantity(s, cart_id, "C-M", 1, clock=clock)
    reserve_cart(s, cart_id, clock=clock, ttl=TTL)
    write(s, update(Variant).where(Variant.sku == "C-M").values(status="archived"))  # archived while held
    version = cart_row(s, cart_id)[0]

    change = remove_line(s, cart_id, "A-M", clock=clock)

    assert change.ended_reservation is not None
    assert change.ended_reservation.status == "released"
    assert [(line.sku, line.available) for line in change.cart.lines] == [("A-S", True), ("C-M", False)]
    assert cart_row(s, cart_id)[0] == version + 1
    assert (reserved(s, "A-S"), reserved(s, "C-M")) == (0, 0)  # archived C-M released too
    check(s)


def test_removing_an_archived_line(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 1)
    set_quantity(s, cart_id, "A-M", 1, clock=clock)
    write(
        s,
        update(Variant).where(Variant.sku == "A-M").values(status="archived"),
        update(Product).where(Product.slug == "shell").values(status="archived"),
    )
    change = remove_line(s, cart_id, "A-M", clock=clock)
    assert [line.sku for line in change.cart.lines] == ["A-S"]
    assert lines(s, cart_id) == {"A-S": 1}


@pytest.mark.parametrize("sku", ["A-M", "NOPE"], ids=["not-in-cart", "unknown-sku"])
def test_removing_a_missing_line_is_a_no_op(s: Session, clock: FixedClock, sku: str) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    reservation = reserve_cart(s, cart_id, clock=clock, ttl=TTL).reservation
    before = cart_row(s, cart_id)
    movements = count(s, InventoryMovement)

    change = remove_line(s, cart_id, sku, clock=clock)
    again = remove_line(s, cart_id, sku, clock=clock)

    assert change == again
    assert change.ended_reservation is None
    assert cart_row(s, cart_id) == before  # no version advance
    assert reservation_statuses(s) == ["active"]
    assert active_public_id(s) == reservation.public_id
    assert count(s, InventoryMovement) == movements
    assert lines(s, cart_id) == {"A-S": 2}
    check(s)


def active_public_id(session: Session) -> str:
    return look(
        session,
        lambda: session.execute(select(Reservation.public_id).where(Reservation.status == "active")).scalar_one(),
    )


# --- unchanged quantity: a true no-op (repeated PUT) ----------------------------------------------


def test_unchanged_quantity_is_a_no_op_that_keeps_the_reservation(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    set_quantity(s, cart_id, "A-M", 1, clock=clock)
    reservation = reserve_cart(s, cart_id, clock=clock, ttl=TTL).reservation
    before = cart_row(s, cart_id)
    movements = count(s, InventoryMovement)
    clock.advance(timedelta(minutes=5))

    first = set_quantity(s, cart_id, "A-S", 2, clock=clock)
    again = set_quantity(s, cart_id, "A-S", 2, clock=clock)

    assert first == again  # the same desired state, every time
    assert first.ended_reservation is None
    assert first.new_token is None
    assert (first.cart.version, first.cart.expires_at) == (before[0], before[2])
    assert cart_row(s, cart_id) == before  # version, updated_at and expiry untouched
    assert reservation_statuses(s) == ["active"]
    assert active_public_id(s) == reservation.public_id
    assert (reserved(s, "A-S"), reserved(s, "A-M")) == (2, 1)  # the hold is intact
    assert count(s, InventoryMovement) == movements  # no movements
    assert count(s, Reservation) == 1  # no new reservation
    assert lines(s, cart_id) == {"A-S": 2, "A-M": 1}
    line_updated = look(
        s,
        lambda: s.execute(
            select(CartItem.updated_at).join(Variant, Variant.id == CartItem.variant_id).where(Variant.sku == "A-S")
        ).scalar_one(),
    )
    assert line_updated == NOW
    assert not s.in_transaction()
    check(s)


def test_a_real_change_after_a_no_op_still_releases(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    reserve_cart(s, cart_id, clock=clock, ttl=TTL)
    set_quantity(s, cart_id, "A-S", 2, clock=clock)  # no-op
    change = set_quantity(s, cart_id, "A-S", 3, clock=clock)  # genuine change
    assert change.ended_reservation is not None
    assert change.ended_reservation.status == "released"
    assert (reservation_statuses(s), reserved(s, "A-S"), lines(s, cart_id)) == (["released"], 0, {"A-S": 3})
    assert change.cart.version == 3
    check(s)


def test_unchanged_quantity_without_a_reservation(s: Session, clock: FixedClock) -> None:
    cart_id = new_cart(s, clock, "A-S", 2)
    before = cart_row(s, cart_id)
    change = set_quantity(s, cart_id, "A-S", 2, clock=clock)
    assert cart_row(s, cart_id) == before
    assert [(line.sku, line.quantity) for line in change.cart.lines] == [("A-S", 2)]
    assert count(s, InventoryMovement) == 0
    check(s)
