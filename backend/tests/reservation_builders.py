"""Builders for reservation tests: carts, unheld reservations, and stock/audit readers.

Reservations built here are inserted directly (active, with items, not yet held), as the
reservation service does inside one transaction before calling hold_reservation_stock.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.cart.models import CART_IDLE_LIFETIME, Cart
from app.domain.catalog.models import Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.reservations.models import Reservation, ReservationItem

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
TTL = timedelta(minutes=15)


def new_cart(session: Session, seed: str) -> Cart:
    cart = Cart(
        token_hash=hashlib.sha256(seed.encode()).hexdigest(),
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + CART_IDLE_LIFETIME,
    )
    session.add(cart)
    session.flush()
    return cart


def variant_ids(session: Session, *skus: str) -> list[int]:
    by_sku = dict(session.execute(select(Variant.sku, Variant.id).where(Variant.sku.in_(skus))).all())
    return [by_sku[sku] for sku in skus]


def new_reservation(session: Session, cart: Cart, items: dict[str, int], seed: str = "r") -> int:
    """An active reservation with items, not yet held (the service writes both in one transaction)."""
    reservation = Reservation(
        public_id=hashlib.md5(f"{cart.id}-{seed}".encode(), usedforsecurity=False).hexdigest(),
        cart_id=cart.id,
        cart_version=cart.version,
        status="active",
        created_at=NOW,
        expires_at=NOW + TTL,
    )
    session.add(reservation)
    session.flush()
    for variant_id, quantity in zip(variant_ids(session, *items), items.values(), strict=True):
        session.add(ReservationItem(reservation_id=reservation.id, variant_id=variant_id, quantity=quantity))
    session.flush()
    return reservation.id


def end(session: Session, reservation_id: int, status: str) -> None:
    """The service's compare-and-set, done directly."""
    changed = session.execute(
        update(Reservation)
        .where(Reservation.id == reservation_id, Reservation.status == "active")
        .values(status=status, ended_at=NOW + TTL)
        .execution_options(synchronize_session=False)
    ).rowcount  # type: ignore[attr-defined]
    assert changed == 1


def stock(session: Session, sku: str) -> tuple[int, int]:
    session.expire_all()
    row = session.execute(select(Variant.stock_on_hand, Variant.stock_reserved).where(Variant.sku == sku)).one()
    return row.stock_on_hand, row.stock_reserved


def trail(session: Session, reservation_id: int) -> list[tuple[str, str, int, int, int]]:
    rows = session.execute(
        select(
            Variant.sku,
            InventoryMovement.reason,
            InventoryMovement.delta_reserved,
            InventoryMovement.on_hand_after,
            InventoryMovement.reserved_after,
        )
        .join(Variant, Variant.id == InventoryMovement.variant_id)
        .where(InventoryMovement.reservation_id == reservation_id)
        .order_by(InventoryMovement.id)
    ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


def movement_count(session: Session) -> int:
    return len(session.execute(select(InventoryMovement.id)).all())
