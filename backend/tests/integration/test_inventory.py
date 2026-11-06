import threading

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.domain.catalog.models import Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.inventory.service import (
    InsufficientAvailableStock,
    InvalidMovementReason,
    InvalidQuantity,
    VariantInactive,
    VariantNotFound,
    adjust_stock,
    movements_for,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories import V, make_product


def variant(session: Session, sku: str) -> Variant:
    session.expire_all()
    return session.execute(select(Variant).where(Variant.sku == sku)).scalar_one()


@pytest.fixture
def stocked(db_session: Session) -> Session:
    make_product(
        db_session,
        "stocked",
        variants=[V(sku="INV-M", size="M", on_hand=5), V(sku="INV-L", size="L", on_hand=0, status="archived")],
    )
    db_session.commit()
    return db_session


def test_restock_increases_stock_and_records_movement(stocked: Session) -> None:
    movement = adjust_stock(stocked, "INV-M", 7, "restock", ref="po-1", note="supplier delivery")
    stocked.commit()
    assert variant(stocked, "INV-M").stock_on_hand == 12
    assert (movement.delta_on_hand, movement.on_hand_after, movement.reserved_after) == (7, 12, 0)
    assert (movement.reason, movement.ref) == ("restock", "po-1")


def test_negative_adjustment_within_available(stocked: Session) -> None:
    adjust_stock(stocked, "INV-M", -5, "adjustment")
    stocked.commit()
    current = variant(stocked, "INV-M")
    assert current.stock_on_hand == 0
    assert current.available == 0


def test_adjustment_cannot_make_available_negative(stocked: Session) -> None:
    # Simulate an M2 reservation of 3 units directly.
    stocked.execute(update(Variant).where(Variant.sku == "INV-M").values(stock_reserved=3))
    stocked.commit()
    with pytest.raises(InsufficientAvailableStock) as caught:
        adjust_stock(stocked, "INV-M", -3, "adjustment")
    assert (caught.value.requested, caught.value.available) == (3, 2)
    stocked.rollback()
    current = variant(stocked, "INV-M")
    assert (current.stock_on_hand, current.stock_reserved) == (5, 3)
    assert movements_for(stocked, "INV-M") == []


@pytest.mark.parametrize(
    ("delta", "reason", "error"),
    [
        (0, "adjustment", InvalidQuantity),
        (True, "adjustment", InvalidQuantity),
        (1.5, "adjustment", InvalidQuantity),
        (-1, "restock", InvalidQuantity),
        (-1, "initial_stock", InvalidQuantity),
        (1, "sale", InvalidMovementReason),
    ],
)
def test_invalid_changes_rejected_without_side_effects(
    stocked: Session, delta: object, reason: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        adjust_stock(stocked, "INV-M", delta, reason)  # type: ignore[arg-type]
    stocked.rollback()
    assert variant(stocked, "INV-M").stock_on_hand == 5
    assert movements_for(stocked, "INV-M") == []


def test_unknown_and_inactive_variants(stocked: Session) -> None:
    with pytest.raises(VariantNotFound):
        adjust_stock(stocked, "NOPE-1", 1, "restock")
    with pytest.raises(VariantInactive):
        adjust_stock(stocked, "INV-L", 1, "restock")


def test_movement_trail_is_ordered_and_matches_stock(stocked: Session) -> None:
    for delta in (3, -2, 10, -6):
        adjust_stock(stocked, "INV-M", delta, "adjustment" if delta < 0 else "restock")
    stocked.commit()
    trail = movements_for(stocked, "INV-M")
    assert [movement.delta_on_hand for movement in trail] == [3, -2, 10, -6]
    assert [movement.on_hand_after for movement in trail] == [8, 6, 16, 10]
    assert variant(stocked, "INV-M").stock_on_hand == 5 + sum(movement.delta_on_hand for movement in trail)


def test_failed_change_rolls_back_with_its_movement(stocked: Session) -> None:
    adjust_stock(stocked, "INV-M", 2, "restock")
    stocked.rollback()  # caller abandons the unit of work
    assert variant(stocked, "INV-M").stock_on_hand == 5
    assert stocked.query(InventoryMovement).count() == 0


def test_database_check_is_a_backstop(stocked: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        stocked.execute(update(Variant).where(Variant.sku == "INV-M").values(stock_on_hand=-1))
    stocked.rollback()


@pytest.mark.postgres
@pytest.mark.skipif(not TEST_DATABASE_URL, reason="real concurrency needs PostgreSQL (TEST_DATABASE_URL)")
def test_concurrent_decrements_never_oversell(migrated_database_url: str) -> None:
    engine = create_db_engine(migrated_database_url)
    with Session(engine) as session:
        make_product(session, "last-unit", variants=[V(sku="LAST-1", on_hand=1)])
        session.commit()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def take_last_unit() -> None:
        with Session(engine) as session:
            barrier.wait()
            try:
                adjust_stock(session, "LAST-1", -1, "adjustment")
                session.commit()
                outcomes.append("ok")
            except InsufficientAvailableStock:
                session.rollback()
                outcomes.append("refused")

    threads = [threading.Thread(target=take_last_unit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    with Session(engine) as session:
        assert sorted(outcomes) == ["ok", "refused"]
        assert variant(session, "LAST-1").stock_on_hand == 0
        assert len(movements_for(session, "LAST-1")) == 1
    engine.dispose()
