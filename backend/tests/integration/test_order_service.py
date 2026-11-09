"""Order service (M3.1, ADR-025): creation by adopting the cart's reservation, reading,
cancellation, lazy expiry, the cart lock while an order is open, and the sale primitive.

Runs on SQLite and PostgreSQL. Real races are in test_order_concurrency.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.db import create_db_engine, session_factory, unit_of_work
from app.domain.cart.service import remove_line, set_quantity
from app.domain.catalog.models import Product, Variant
from app.domain.inventory.service import (
    InsufficientAvailableStock,
    InventoryInvariantViolation,
    commit_reservation_stock,
    release_reservation_stock,
)
from app.domain.orders.holds import CheckoutInProgress, expire_order_if_due, sweep_due_orders
from app.domain.orders.models import Order, OrderItem
from app.domain.orders.service import (
    CheckoutConflict,
    OrderNotCancellable,
    OrderNotFound,
    PaymentInProgress,
    ReservationRequired,
    cancel_order,
    checkout_summary,
    create_order,
    get_order,
)
from app.domain.orders.snapshot import SnapshotLine, version_hash
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import (
    CartHasUnavailableItems,
    CartNotFound,
    active_reservation,
    expire_reservation,
    release_cart_reservation,
    reserve_cart,
    sweep_overdue,
)
from app.domain.shipping.service import InvalidCountryCode, UnsupportedDestination
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.order_support import (
    EU_SHIPPING_MINOR,
    HOLD,
    add_shipping,
    cart_reservation_id,
    cart_version,
    count,
    look,
    order_row,
    reasons,
    reservation_status_of,
    stock,
    variant_id,
)
from tests.reservation_builders import NOW, TTL


@pytest.fixture
def factory(migrated_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(migrated_database_url)
    factory = session_factory(engine)
    with factory() as session:
        make_product(
            session,
            "shell",
            translations={"en": ("Rain Shell", "Waterproof."), "es": ("Chaqueta", "Impermeable.")},
            variants=[V(sku="A-S", size="S", price_minor=12900, on_hand=5), V(sku="A-M", price_minor=13900, on_hand=2)],
        )
        make_product(session, "sock", category="socks", variants=[V(sku="C-M", price_minor=1600, on_hand=4)])
        add_shipping(session)
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def s(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
        session.rollback()
        assert_inventory_invariants(session)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def cart_with(s: Session, clock: FixedClock, lines: dict[str, int], *, reserve: bool = True) -> int:
    cart_id: int | None = None
    for sku, quantity in lines.items():
        cart_id = set_quantity(s, cart_id, sku, quantity, clock=clock).cart.cart_id
    assert cart_id is not None
    if reserve:
        reserve_cart(s, cart_id, clock=clock, ttl=TTL)
    return cart_id


def order_for(s: Session, cart_id: int, clock: FixedClock, country: str = "FR"):  # type: ignore[no-untyped-def]
    return create_order(s, cart_id, ship_country=country, lang="es", clock=clock, hold=HOLD)


# --- creation ----------------------------------------------------------------------------------


def test_create_adopts_the_reservation_without_reserving_again(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 2, "C-M": 1})
    before = reasons(s)
    assert (stock(s, "A-S"), stock(s, "C-M")) == ((5, 2), (4, 1))

    result = order_for(s, cart_id, clock)

    assert result.created
    order = result.order
    assert order.status == "pending_approval"
    assert (order.subtotal_minor, order.shipping_minor) == (2 * 12900 + 1600, EU_SHIPPING_MINOR)
    assert order.total_minor == order.subtotal_minor + order.shipping_minor
    assert (order.currency, order.ship_country, order.shipping_zone, order.lang) == ("USD", "FR", "europe", "es")
    assert order.hold_expires_at == NOW + HOLD
    assert [(line.sku, line.quantity, line.unit_price_minor, line.line_total_minor) for line in order.lines] == [
        ("A-S", 2, 12900, 25800),
        ("C-M", 1, 1600, 1600),
    ]
    assert order.lines[0].name_snapshot == "Rain Shell"  # English, whatever the order language
    # The same stock stays reserved: no new hold, no movement at all.
    assert (stock(s, "A-S"), stock(s, "C-M")) == ((5, 2), (4, 1))
    assert reasons(s) == before
    assert reservation_status_of(s, order.public_ref) == "order_hold"
    assert active_reservation(s, cart_id, clock=clock) is None  # no longer the cart's to end


def test_version_hash_matches_the_stored_snapshot(s: Session, clock: FixedClock) -> None:
    order = order_for(s, cart_with(s, clock, {"A-M": 1, "C-M": 2}), clock).order
    row = look(s, lambda: s.execute(select(Order).where(Order.public_ref == order.public_ref)).scalar_one())
    items = look(s, lambda: s.execute(select(OrderItem).where(OrderItem.order_id == row.id)).scalars().all())
    recomputed = version_hash(
        public_ref=row.public_ref,
        currency=row.currency,
        ship_country=row.ship_country,
        lines=[SnapshotLine(i.sku, i.quantity, i.unit_price_minor, i.line_total_minor) for i in items],
        subtotal_minor=row.subtotal_minor,
        shipping_minor=row.shipping_minor,
        total_minor=row.total_minor,
    )
    assert row.version_hash == order.version_hash == recomputed


def test_snapshot_is_immune_to_later_price_changes(s: Session, clock: FixedClock) -> None:
    order = order_for(s, cart_with(s, clock, {"A-S": 1}), clock).order
    with unit_of_work(s):
        s.execute(update(Variant).where(Variant.sku == "A-S").values(price_minor=99900))
    again = get_order(s, order_row(s, order.public_ref).cart_id, order.public_ref, clock=clock)
    assert (again.lines[0].unit_price_minor, again.total_minor, again.version_hash) == (
        12900,
        order.total_minor,
        order.version_hash,
    )


def test_retrying_create_returns_the_same_open_order(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 1})
    first = order_for(s, cart_id, clock)
    retry = order_for(s, cart_id, clock)  # e.g. the client timed out after the first committed
    other_country = order_for(s, cart_id, clock, country="DE")
    assert first.created
    assert not retry.created
    assert not other_country.created
    assert retry.order == first.order == other_country.order  # the open order, unchanged
    assert count(s, Order) == 1
    summary = checkout_summary(s, cart_id, clock=clock)
    assert summary is not None
    assert (summary.public_ref, summary.status) == (first.order.public_ref, "pending_approval")


def test_create_needs_an_unexpired_reservation_for_the_current_cart(s: Session, clock: FixedClock) -> None:
    unreserved = cart_with(s, clock, {"A-S": 1}, reserve=False)
    with pytest.raises(ReservationRequired):
        order_for(s, unreserved, clock)

    reserved = cart_with(s, clock, {"C-M": 1})
    with pytest.raises(ReservationRequired):
        order_for(s, reserved, FixedClock(NOW + TTL))  # the cart hold has lapsed

    edited = cart_with(s, clock, {"A-M": 1})
    set_quantity(s, edited, "A-M", 2, clock=clock)  # an edit releases the reservation
    with pytest.raises(ReservationRequired):
        order_for(s, edited, clock)
    assert count(s, Order) == 0


def test_create_for_an_unknown_cart(s: Session, clock: FixedClock) -> None:
    with pytest.raises(CartNotFound):
        order_for(s, 999_999, clock)


@pytest.mark.parametrize(("country", "error"), [("US", UnsupportedDestination), ("F", InvalidCountryCode)])
def test_bad_destination_writes_nothing(s: Session, clock: FixedClock, country: str, error: type) -> None:
    cart_id = cart_with(s, clock, {"A-S": 1})
    version = cart_version(s, cart_id)
    with pytest.raises(error):
        order_for(s, cart_id, clock, country=country)
    assert count(s, Order) == 0
    assert cart_version(s, cart_id) == version  # the cart lock was rolled back too
    assert active_reservation(s, cart_id, clock=clock) is not None


def test_archived_item_after_reservation_blocks_the_order(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 1, "C-M": 1})
    with unit_of_work(s):
        s.execute(update(Product).where(Product.slug == "sock").values(status="archived"))
    with pytest.raises(CartHasUnavailableItems) as raised:
        order_for(s, cart_id, clock)
    assert raised.value.details == [{"sku": "C-M"}]
    assert count(s, Order) == 0
    assert active_reservation(s, cart_id, clock=clock) is not None  # still the cart's


def test_lost_cart_race_with_no_order_is_a_conflict(
    s: Session, factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.orders import service

    cart_id = cart_with(s, clock, {"A-S": 1})
    real = service._create_locked

    def edit_first(session: Session, cart: int, **kwargs: object) -> int:
        with factory() as other:  # a competing edit commits first
            set_quantity(other, cart, "A-S", 2, clock=clock)
        return real(session, cart, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_create_locked", edit_first)
    with pytest.raises(CheckoutConflict):
        order_for(s, cart_id, clock)
    assert count(s, Order) == 0


# --- the cart while an order is open -----------------------------------------------------------


def test_cart_changes_are_refused_while_an_order_is_open(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 2})
    ref = order_for(s, cart_id, clock).order.public_ref
    version = cart_version(s, cart_id)
    before = reasons(s)

    for change in (
        lambda: set_quantity(s, cart_id, "A-S", 3, clock=clock),
        lambda: set_quantity(s, cart_id, "A-S", 2, clock=clock),  # even an unchanged quantity
        lambda: set_quantity(s, cart_id, "C-M", 1, clock=clock),
        lambda: remove_line(s, cart_id, "A-S", clock=clock),
        lambda: reserve_cart(s, cart_id, clock=clock, ttl=TTL),
        lambda: release_cart_reservation(s, cart_id, clock=clock),
    ):
        with pytest.raises(CheckoutInProgress) as raised:
            change()
        assert raised.value.details == [{"order_ref": ref}]

    assert cart_version(s, cart_id) == version
    assert reasons(s) == before
    assert stock(s, "A-S") == (5, 2)
    assert reservation_status_of(s, ref) == "order_hold"


def test_cart_side_expiry_and_sweeps_never_end_an_order_hold(
    s: Session, factory: sessionmaker[Session], clock: FixedClock
) -> None:
    cart_id = cart_with(s, clock, {"A-M": 2})  # all of A-M's stock
    ref = order_for(s, cart_id, clock).order.public_ref
    reservation_id = order_row(s, ref).reservation_id
    late = FixedClock(NOW + TTL + timedelta(minutes=1))  # past the cart TTL, before the order hold

    assert active_reservation(s, cart_id, clock=late) is None
    assert expire_reservation(s, reservation_id, clock=late) is False
    with factory() as other:
        rival = set_quantity(other, None, "C-M", 1, clock=late).cart.cart_id
        with pytest.raises(InsufficientAvailableStock):  # it sweeps A-M first, and the hold stays
            set_quantity(other, rival, "A-M", 1, clock=late)
    assert sweep_overdue(s, variant_ids=[variant_id(s, "A-M")], exclude_cart_id=None, clock=late) == 0
    assert sweep_due_orders(s, variant_ids=[variant_id(s, "A-M")], exclude_cart_id=None, clock=late) == 0
    assert reservation_status_of(s, ref) == "order_hold"
    assert stock(s, "A-M") == (2, 2)


# --- cancellation and expiry -------------------------------------------------------------------


def test_cancel_releases_the_hold_once_and_unlocks_the_cart(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 2})
    ref = order_for(s, cart_id, clock).order.public_ref

    cancelled = cancel_order(s, cart_id, ref, clock=clock)
    again = cancel_order(s, cart_id, ref, clock=clock)  # idempotent

    assert cancelled.status == again.status == "cancelled"
    assert reservation_status_of(s, ref) == "released"
    assert reasons(s) == ["reservation_hold", "reservation_release"]
    assert stock(s, "A-S") == (5, 0)
    assert checkout_summary(s, cart_id, clock=clock) is None
    set_quantity(s, cart_id, "A-S", 3, clock=clock)  # the bag can change again
    reserve_cart(s, cart_id, clock=clock, ttl=TTL)
    second = order_for(s, cart_id, clock)
    assert second.created
    assert second.order.public_ref != ref


def test_cancel_after_the_deadline_records_expiry(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 1})
    ref = order_for(s, cart_id, clock).order.public_ref
    assert cancel_order(s, cart_id, ref, clock=FixedClock(NOW + HOLD)).status == "expired"
    assert reservation_status_of(s, ref) == "expired"
    assert reasons(s) == ["reservation_hold", "reservation_expiry"]


def test_hold_expires_lazily_on_read_exactly_once(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 2})
    ref = order_for(s, cart_id, clock).order.public_ref
    order_id = order_row(s, ref).id

    almost = FixedClock(NOW + HOLD - timedelta(seconds=1))
    assert get_order(s, cart_id, ref, clock=almost).status == "pending_approval"
    assert expire_order_if_due(s, order_id, clock=almost) is False

    late = FixedClock(NOW + HOLD)
    assert get_order(s, cart_id, ref, clock=late).status == "expired"
    assert expire_order_if_due(s, order_id, clock=late) is False  # already ended
    assert reservation_status_of(s, ref) == "expired"
    assert reasons(s) == ["reservation_hold", "reservation_expiry"]
    assert stock(s, "A-S") == (5, 0)
    set_quantity(s, cart_id, "A-S", 1, clock=late)  # unlocked


def test_a_cart_edit_after_the_deadline_expires_the_order_first(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 2})
    ref = order_for(s, cart_id, clock).order.public_ref
    set_quantity(s, cart_id, "A-S", 1, clock=FixedClock(NOW + HOLD))
    assert order_row(s, ref).status == "expired"


def test_another_cart_sweeps_an_abandoned_order(s: Session, clock: FixedClock) -> None:
    owner = cart_with(s, clock, {"A-M": 2})  # all of A-M
    ref = order_for(s, owner, clock).order.public_ref
    late = FixedClock(NOW + HOLD)
    rival = cart_with(s, late, {"A-M": 2})  # its add and reservation sweep the abandoned hold
    assert order_row(s, ref).status == "expired"
    assert active_reservation(s, rival, clock=late) is not None
    assert stock(s, "A-M") == (2, 2)


def test_order_sweep_is_bounded_skips_the_caller_and_never_releases_twice(
    s: Session, factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.orders import holds

    refs = [order_for(s, cart_with(s, clock, {"C-M": 1}), clock).order.public_ref for _ in range(4)]
    carts = [order_row(s, ref).cart_id for ref in refs]
    late = FixedClock(NOW + HOLD)
    c_m = [variant_id(s, "C-M")]

    # Bounded, and the requesting cart's own order is excluded (it has its own lazy expiry).
    assert sweep_due_orders(s, variant_ids=c_m, exclude_cart_id=carts[0], clock=late, limit=2) == 2
    assert [order_row(s, ref).status for ref in refs] == ["pending_approval", "expired", "expired", "pending_approval"]

    # A candidate that is cancelled between the sweep's read and its compare-and-set is skipped:
    # its stock is released once, by the cancellation.
    real = holds.expire_order_if_due

    def cancelled_meanwhile(session: Session, order_id: int, *, clock: FixedClock) -> bool:
        with factory() as other:
            cancel_order(other, carts[3], refs[3], clock=clock)
        return real(session, order_id, clock=clock)

    monkeypatch.setattr(holds, "expire_order_if_due", cancelled_meanwhile)
    assert sweep_due_orders(s, variant_ids=c_m, exclude_cart_id=carts[0], clock=late) == 0
    assert order_row(s, refs[3]).status == "expired"  # cancel past the deadline records expiry
    assert reasons(s).count("reservation_expiry") == 3  # orders 1, 2 and 3: once each
    assert stock(s, "C-M") == (4, 1)  # only order 0's hold remains


@pytest.mark.parametrize("status", ["capturing", "payment_pending", "needs_review"])
def test_payment_in_progress_keeps_the_hold(s: Session, clock: FixedClock, status: str) -> None:
    cart_id = cart_with(s, clock, {"A-S": 1})
    ref = order_for(s, cart_id, clock).order.public_ref
    with unit_of_work(s):  # M3.2 sets these; M3.1 only has to respect them
        s.execute(update(Order).where(Order.public_ref == ref).values(status=status))
    late = FixedClock(NOW + HOLD + timedelta(hours=1))

    with pytest.raises(PaymentInProgress):
        cancel_order(s, cart_id, ref, clock=late)
    assert get_order(s, cart_id, ref, clock=late).status == status  # never expired lazily
    assert sweep_due_orders(s, variant_ids=[variant_id(s, "A-S")], exclude_cart_id=None, clock=late) == 0
    with pytest.raises(CheckoutInProgress):
        set_quantity(s, cart_id, "A-S", 2, clock=late)
    assert reservation_status_of(s, ref) == "order_hold"
    assert stock(s, "A-S") == (5, 1)


def test_final_orders_are_not_cancellable(s: Session, clock: FixedClock) -> None:
    cart_id = cart_with(s, clock, {"A-S": 1})
    ref = order_for(s, cart_id, clock).order.public_ref
    order = order_row(s, ref)
    with unit_of_work(s):  # what M3.2's finalisation will do, in one transaction
        s.execute(
            update(Reservation).where(Reservation.id == order.reservation_id).values(status="committed", ended_at=NOW)
        )
        commit_reservation_stock(s, order.reservation_id, now=NOW)
        s.execute(update(Order).where(Order.id == order.id).values(status="paid"))
    with pytest.raises(OrderNotCancellable):
        cancel_order(s, cart_id, ref, clock=clock)
    assert checkout_summary(s, cart_id, clock=clock) is None


# --- ownership ---------------------------------------------------------------------------------


def test_orders_are_only_visible_to_their_own_cart(s: Session, clock: FixedClock) -> None:
    owner = cart_with(s, clock, {"A-S": 1})
    stranger = cart_with(s, clock, {"C-M": 1})
    ref = order_for(s, owner, clock).order.public_ref
    with pytest.raises(OrderNotFound):
        get_order(s, stranger, ref, clock=clock)
    with pytest.raises(OrderNotFound):
        cancel_order(s, stranger, ref, clock=clock)
    with pytest.raises(OrderNotFound):
        get_order(s, owner, "f" * 32, clock=clock)
    assert order_row(s, ref).status == "pending_approval"


# --- the sale primitive (used by M3.2's payment finalisation) ----------------------------------


def adopted(s: Session, clock: FixedClock, lines: dict[str, int]) -> int:
    return order_row(s, order_for(s, cart_with(s, clock, lines), clock).order.public_ref).reservation_id


def test_commit_sells_the_held_stock_once(s: Session, clock: FixedClock) -> None:
    reservation_id = adopted(s, clock, {"A-S": 2, "C-M": 1})
    with unit_of_work(s):
        s.execute(update(Reservation).where(Reservation.id == reservation_id).values(status="committed", ended_at=NOW))
        commit_reservation_stock(s, reservation_id, now=NOW)
    assert (stock(s, "A-S"), stock(s, "C-M")) == ((3, 0), (3, 0))
    assert reasons(s)[-2:] == ["sale", "sale"]

    with pytest.raises(InventoryInvariantViolation, match="already released or sold"), unit_of_work(s):
        commit_reservation_stock(s, reservation_id, now=NOW)
    with pytest.raises(InventoryInvariantViolation, match="committed, not released"), unit_of_work(s):
        release_reservation_stock(s, reservation_id, now=NOW)
    with unit_of_work(s):  # keep the invariants' order <-> reservation pairing consistent
        s.execute(update(Order).where(Order.reservation_id == reservation_id).values(status="paid"))


@pytest.mark.parametrize("status", ["active", "order_hold", "released", "expired"])
def test_commit_refuses_anything_but_a_committed_reservation(s: Session, clock: FixedClock, status: str) -> None:
    if status == "active":
        reservation_id = cart_reservation_id(s, cart_with(s, clock, {"A-S": 1}))
    else:
        reservation_id = adopted(s, clock, {"A-S": 1})
        if status != "order_hold":
            ref = look(
                s,
                lambda: s.execute(select(Order.public_ref).where(Order.reservation_id == reservation_id)).scalar_one(),
            )
            cancel_order(
                s, order_row(s, ref).cart_id, ref, clock=FixedClock(NOW + HOLD if status == "expired" else NOW)
            )
    with pytest.raises(InventoryInvariantViolation), unit_of_work(s):
        commit_reservation_stock(s, reservation_id, now=NOW)
    if status in ("active", "order_hold"):
        with pytest.raises(InventoryInvariantViolation, match=f"still {status}"), unit_of_work(s):
            release_reservation_stock(s, reservation_id, now=NOW)
