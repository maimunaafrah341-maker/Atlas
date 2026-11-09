"""Shared builders and readers for the order tests (M3.1).

Every reader closes its read transaction before returning (like ``look`` in
test_reservation_service.py), so the next service call can start its own unit of work, and
returns plain values rather than ORM objects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.cart.models import Cart
from app.domain.catalog.models import Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.orders.models import Order
from app.domain.reservations.models import Reservation
from app.domain.shipping.models import ShippingRate, ShippingRateCountry

T = TypeVar("T")

HOLD = timedelta(minutes=30)
EU_SHIPPING_MINOR = 1500


def add_shipping(session: Session) -> None:
    """One flat-rate zone (FR, DE). Every other country is unsupported."""
    rate = ShippingRate(zone_code="europe", name="Europe", price_minor=EU_SHIPPING_MINOR, currency="USD")
    rate.countries = [ShippingRateCountry(country_code="FR"), ShippingRateCountry(country_code="DE")]
    session.add(rate)
    session.flush()


def look(session: Session, read: Callable[[], T]) -> T:
    session.expire_all()
    try:
        return read()
    finally:
        session.rollback()


@dataclass(frozen=True)
class OrderRow:
    id: int
    cart_id: int
    reservation_id: int
    status: str
    reservation_status: str


def order_row(session: Session, public_ref: str) -> OrderRow:
    row = look(
        session,
        lambda: session.execute(
            select(Order.id, Order.cart_id, Order.reservation_id, Order.status, Reservation.status.label("rs"))
            .join(Reservation, Reservation.id == Order.reservation_id)
            .where(Order.public_ref == public_ref)
        ).one(),
    )
    return OrderRow(row.id, row.cart_id, row.reservation_id, row.status, row.rs)


def reservation_status_of(session: Session, public_ref: str) -> str:
    return order_row(session, public_ref).reservation_status


def stock(session: Session, sku: str) -> tuple[int, int]:
    row = look(
        session,
        lambda: session.execute(select(Variant.stock_on_hand, Variant.stock_reserved).where(Variant.sku == sku)).one(),
    )
    return row.stock_on_hand, row.stock_reserved


def variant_id(session: Session, sku: str) -> int:
    return look(session, lambda: session.execute(select(Variant.id).where(Variant.sku == sku)).scalar_one())


def reasons(session: Session) -> list[str]:
    return look(
        session,
        lambda: list(session.execute(select(InventoryMovement.reason).order_by(InventoryMovement.id)).scalars()),
    )


def count(session: Session, model: type) -> int:
    return look(session, lambda: session.execute(select(func.count()).select_from(model)).scalar_one())


def cart_version(session: Session, cart_id: int) -> int:
    return look(session, lambda: session.execute(select(Cart.version).where(Cart.id == cart_id)).scalar_one())


def cart_reservation_id(session: Session, cart_id: int) -> int:
    return look(
        session,
        lambda: session.execute(select(Reservation.id).where(Reservation.cart_id == cart_id)).scalar_one(),
    )
