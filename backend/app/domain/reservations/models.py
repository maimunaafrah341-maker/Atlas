"""Cart stock reservations (M2, ADR-024).

A reservation holds stock for one cart for a fixed TTL (D3, RESERVATION_TTL_MINUTES). It is a
header (status, cart version, expiry) plus items that record each variant and the quantity
held. Item quantities are an immutable snapshot used for release: release never recomputes
quantities from the current cart. Reservations carry no prices; price snapshots belong to
the M3 order (D1).

Lifecycle: ``active`` -> ``released`` (customer release, or any cart edit, D2) or ``expired``
(TTL passed; applied lazily, D6). M3 (ADR-025): creating an order adopts the active reservation
as ``order_hold``; it then ends only through its order, as ``committed`` (verified payment,
M3.2), ``released`` (cancellation or definitive payment failure) or ``expired`` (the order's
hold deadline passed before capture). Cart-side expiry, release and sweeps match only
``active``, so they can never end an ``order_hold``. ``committed``, ``released`` and ``expired``
are final. Every hold, release, expiry and sale writes inventory_movements rows, linked by
``reservation_id``, in the same transaction as the change to the variant's stock.
Reservations and their items are audit history: they are never deleted, which is why every
foreign key into or out of these tables is RESTRICT.

A cart has at most one active reservation (partial unique index, SQLite and PostgreSQL), and
``cart_version`` records the cart version it was made for.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime
from app.domain.cart.models import MAX_QUANTITY_PER_LINE
from app.domain.catalog.vocab import sql_in

RESERVATION_STATUSES: tuple[str, ...] = ("active", "order_hold", "committed", "released", "expired")
# Statuses that still hold stock (counted in variants.stock_reserved) and have not ended.
HOLDING_STATUSES: tuple[str, ...] = ("active", "order_hold")
PUBLIC_ID_LENGTH = 32  # uuid4().hex

ACTIVE_ONLY = text("status = 'active'")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(PUBLIC_ID_LENGTH), unique=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="RESTRICT"))
    cart_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    items: Mapped[list[ReservationItem]] = relationship(
        back_populates="reservation", order_by="ReservationItem.variant_id"
    )

    __table_args__ = (
        CheckConstraint(sql_in("status", RESERVATION_STATUSES), name="status"),
        CheckConstraint(f"length(public_id) = {PUBLIC_ID_LENGTH}", name="public_id_length"),
        CheckConstraint("cart_version >= 0", name="cart_version_non_negative"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        # Holding reservations have not ended; ended ones record when.
        CheckConstraint(
            f"({sql_in('status', HOLDING_STATUSES)} AND ended_at IS NULL)"
            f" OR (status NOT IN ('active', 'order_hold') AND ended_at IS NOT NULL)",
            name="ended_at_matches_status",
        ),
        Index(
            "uq_reservations_cart_id_active",
            "cart_id",
            unique=True,
            sqlite_where=ACTIVE_ONLY,
            postgresql_where=ACTIVE_ONLY,
        ),
        Index("ix_reservations_status_expires_at", "status", "expires_at"),
    )


class ReservationItem(Base):
    __tablename__ = "reservation_items"

    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="RESTRICT"), primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)

    reservation: Mapped[Reservation] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint(f"quantity BETWEEN 1 AND {MAX_QUANTITY_PER_LINE}", name="quantity_range"),
        Index("ix_reservation_items_variant_id", "variant_id"),
    )
