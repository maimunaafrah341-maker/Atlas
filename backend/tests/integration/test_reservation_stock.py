"""Reservation hold and release primitives (ARCHITECTURE §3.1, ADR-024).

The reservation service (later in M2.2) owns the status compare-and-sets; here the tests make
those transitions directly, then check what the primitives do with stock and the audit trail.
Concurrency tests at the end run only on PostgreSQL.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.db import create_db_engine, session_factory
from app.domain.cart.models import Cart
from app.domain.catalog.models import Product, Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.inventory.service import (
    InsufficientAvailableStock,
    InventoryInvariantViolation,
    StockShortfall,
    adjust_stock,
    hold_reservation_stock,
    release_reservation_stock,
)
from app.domain.reservations.models import Reservation
from tests.conftest import TEST_DATABASE_URL
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.reservation_builders import (
    NOW,
    TTL,
    end,
    movement_count,
    new_cart,
    new_reservation,
    stock,
    trail,
    variant_ids,
)


@pytest.fixture
def shop(db_session: Session) -> Session:
    make_product(
        db_session,
        "shell",
        variants=[
            V(sku="A-S", size="S", on_hand=5),
            V(sku="A-M", size="M", on_hand=2),
            V(sku="A-L", size="L", on_hand=0),
            V(sku="A-XL", size="XL", on_hand=4, status="archived"),
        ],
    )
    make_product(db_session, "retired", status="archived", variants=[V(sku="B-M", on_hand=3)])
    db_session.commit()
    return db_session


# --- holds ---------------------------------------------------------------------------------


def test_hold_reserves_every_item_with_linked_movements(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    # Inserted in descending id order; holds still run (and are recorded) in ascending order.
    reservation_id = new_reservation(shop, cart, {"A-M": 2, "A-S": 3})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.commit()

    assert stock(shop, "A-S") == (5, 3)
    assert stock(shop, "A-M") == (2, 2)
    assert trail(shop, reservation_id) == [
        ("A-S", "reservation_hold", 3, 5, 3),
        ("A-M", "reservation_hold", 2, 2, 2),
    ]
    times = shop.execute(select(InventoryMovement.created_at)).scalars().all()
    assert {t.replace(tzinfo=UTC) if t.tzinfo is None else t for t in times} == {NOW}
    assert_inventory_invariants(shop)


def test_shortfall_names_every_short_variant_and_rolls_back_completely(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    shop.commit()
    reservation_id = new_reservation(shop, cart, {"A-S": 1, "A-M": 3, "A-L": 1})  # A-S fits, the others don't
    with pytest.raises(StockShortfall) as raised:
        hold_reservation_stock(shop, reservation_id, now=NOW)
    assert set(raised.value.variant_ids) == set(variant_ids(shop, "A-M", "A-L"))
    # D-5: nothing a client could see carries a count.
    assert raised.value.details == []
    assert raised.value.message == "Some items are not available in the requested quantity."
    shop.rollback()

    assert [stock(shop, sku) for sku in ("A-S", "A-M", "A-L")] == [(5, 0), (2, 0), (0, 0)]
    assert movement_count(shop) == 0
    assert shop.execute(select(Reservation.id)).all() == []  # the reservation rolled back with its holds
    assert_inventory_invariants(shop)


@pytest.mark.parametrize("sku", ["A-XL", "B-M"], ids=["archived-variant", "archived-product"])
def test_archived_stock_cannot_be_held(shop: Session, sku: str) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {sku: 1})
    with pytest.raises(StockShortfall):
        hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.rollback()
    assert stock(shop, sku)[1] == 0


def test_hold_refuses_reservations_that_are_not_fresh_and_active(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {"A-S": 1})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    with pytest.raises(InventoryInvariantViolation, match="already has stock movements"):
        hold_reservation_stock(shop, reservation_id, now=NOW)
    end(shop, reservation_id, "released")
    with pytest.raises(InventoryInvariantViolation, match="not active"):
        hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.rollback()


def test_hold_refuses_a_reservation_without_items(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {})
    with pytest.raises(InventoryInvariantViolation, match="no items"):
        hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.rollback()


def test_held_stock_cannot_be_adjusted_away(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {"A-M": 2})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.commit()
    with pytest.raises(InsufficientAvailableStock):
        adjust_stock(shop, "A-M", -1, "adjustment")
    shop.rollback()
    assert stock(shop, "A-M") == (2, 2)


# --- releases ------------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "reason"), [("released", "reservation_release"), ("expired", "reservation_expiry")])
def test_release_returns_the_held_quantities_with_the_status_reason(shop: Session, status: str, reason: str) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {"A-S": 3, "A-M": 1})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.commit()

    end(shop, reservation_id, status)
    release_reservation_stock(shop, reservation_id, now=NOW + TTL)
    shop.commit()

    assert stock(shop, "A-S") == (5, 0)
    assert stock(shop, "A-M") == (2, 0)
    assert trail(shop, reservation_id)[2:] == [("A-S", reason, -3, 5, 0), ("A-M", reason, -1, 2, 0)]
    assert_inventory_invariants(shop)


def test_release_uses_the_reservation_items_not_anything_else(shop: Session) -> None:
    # Two reservations on the same variant: releasing one returns only its own quantity.
    first = new_reservation(shop, new_cart(shop, "c1"), {"A-S": 2})
    second = new_reservation(shop, new_cart(shop, "c2"), {"A-S": 1})
    hold_reservation_stock(shop, first, now=NOW)
    hold_reservation_stock(shop, second, now=NOW)
    end(shop, first, "released")
    release_reservation_stock(shop, first, now=NOW)
    shop.commit()
    assert stock(shop, "A-S") == (5, 1)
    assert_inventory_invariants(shop)


def test_release_works_after_the_variant_and_product_are_archived(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {"A-S": 2})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.commit()
    shop.execute(update(Variant).where(Variant.sku == "A-S").values(status="archived"))
    shop.execute(update(Product).where(Product.slug == "shell").values(status="archived"))
    shop.commit()

    end(shop, reservation_id, "expired")
    release_reservation_stock(shop, reservation_id, now=NOW + TTL)
    shop.commit()
    assert stock(shop, "A-S") == (5, 0)
    assert_inventory_invariants(shop)


def test_release_refuses_active_unheld_and_already_released_reservations(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {"A-S": 1})
    with pytest.raises(InventoryInvariantViolation, match="still active"):
        release_reservation_stock(shop, reservation_id, now=NOW)

    end(shop, reservation_id, "released")
    with pytest.raises(InventoryInvariantViolation, match="never held"):
        release_reservation_stock(shop, reservation_id, now=NOW)
    shop.rollback()

    reservation_id = new_reservation(shop, new_cart(shop, "c2"), {"A-S": 1})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    end(shop, reservation_id, "released")
    release_reservation_stock(shop, reservation_id, now=NOW)
    shop.commit()
    with pytest.raises(InventoryInvariantViolation, match="already released"):
        release_reservation_stock(shop, reservation_id, now=NOW)
    shop.rollback()
    assert stock(shop, "A-S") == (5, 0)  # never decremented twice
    assert_inventory_invariants(shop)


def test_release_refuses_to_take_reserved_stock_below_zero(shop: Session) -> None:
    cart = new_cart(shop, "c1")
    reservation_id = new_reservation(shop, cart, {"A-S": 2})
    hold_reservation_stock(shop, reservation_id, now=NOW)
    shop.commit()
    # Corrupt the counter behind the services' back.
    shop.execute(update(Variant).where(Variant.sku == "A-S").values(stock_reserved=1))
    end(shop, reservation_id, "released")
    with pytest.raises(InventoryInvariantViolation, match="less reserved stock"):
        release_reservation_stock(shop, reservation_id, now=NOW)
    shop.rollback()
    assert stock(shop, "A-S") == (5, 2)


# --- PostgreSQL concurrency ----------------------------------------------------------------

needs_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="real concurrency needs PostgreSQL (TEST_DATABASE_URL)"
)


def _race(url: str, plans: list[tuple[str, dict[str, int]]]) -> tuple[list[str], list[BaseException]]:
    """Each plan creates and holds its own reservation in one transaction, all starting together."""
    engine = create_db_engine(url)
    barrier = threading.Barrier(len(plans))
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def run(seed: str, items: dict[str, int]) -> None:
        try:
            with session_factory(engine)() as session:
                session.execute(text("SET LOCAL lock_timeout = '10s'"))
                cart = session.execute(
                    select(Cart).where(Cart.token_hash == hashlib.sha256(seed.encode()).hexdigest())
                ).scalar_one()
                reservation_id = new_reservation(session, cart, items, seed)
                barrier.wait(timeout=10)
                try:
                    hold_reservation_stock(session, reservation_id, now=NOW)
                    session.commit()
                    outcomes.append("held")
                except StockShortfall as shortfall:
                    session.rollback()
                    outcomes.append(f"short:{len(shortfall.variant_ids)}")
        except BaseException as error:  # surfaced to the test thread, including deadlocks
            errors.append(error)

    threads = [threading.Thread(target=run, args=plan) for plan in plans]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a hold never finished"
    engine.dispose()
    return outcomes, errors


def _carts(url: str, *seeds: str, variants: list[V]) -> None:
    engine = create_db_engine(url)
    with session_factory(engine)() as session:
        make_product(session, "race", variants=variants)
        for seed in seeds:
            new_cart(session, seed)
        session.commit()
    engine.dispose()


@pytest.mark.postgres
@needs_postgres
def test_concurrent_holds_on_the_last_unit_never_oversell(migrated_database_url: str) -> None:
    _carts(migrated_database_url, "c1", "c2", "c3", "c4", variants=[V(sku="LAST", on_hand=1)])
    outcomes, errors = _race(migrated_database_url, [(seed, {"LAST": 1}) for seed in ("c1", "c2", "c3", "c4")])
    assert errors == []
    assert sorted(outcomes) == ["held", "short:1", "short:1", "short:1"]
    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        assert stock(session, "LAST") == (1, 1)
        assert_inventory_invariants(session)
    engine.dispose()


@pytest.mark.postgres
@needs_postgres
def test_overlapping_multi_variant_holds_do_not_deadlock(migrated_database_url: str) -> None:
    # The two reservations list the variants in opposite orders; holds lock in ascending id
    # order regardless, so one gets both units and the other gets neither.
    _carts(migrated_database_url, "c1", "c2", variants=[V(sku="X", size="S", on_hand=1), V(sku="Y", on_hand=1)])
    outcomes, errors = _race(migrated_database_url, [("c1", {"X": 1, "Y": 1}), ("c2", {"Y": 1, "X": 1})])
    assert errors == []
    assert sorted(outcomes) == ["held", "short:2"]
    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        assert stock(session, "X") == (1, 1)
        assert stock(session, "Y") == (1, 1)
        assert_inventory_invariants(session)
    engine.dispose()
