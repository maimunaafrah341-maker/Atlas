"""Orders and their immutable item snapshots (M3, ADR-008, ADR-025).

An order is created from a cart whose stock is already reserved. It **adopts** that reservation
(``active`` -> ``order_hold``) instead of reserving stock again, and from then on the
reservation ends only through the order. The order fixes the prices, shipping and totals the
customer reviews, in integer USD cents (ADR-003), with a canonical SHA-256 ``version_hash`` over
them (ADR-008, ``app.domain.orders.snapshot``).

Statuses (ARCHITECTURE §5). M3.1 reaches only ``pending_approval``, ``cancelled`` and
``expired``; the payment statuses are listed now so the M3.2 payment migration does not have to
rebuild this table:

- open, before capture: ``pending_approval``, ``approved``, ``awaiting_payment``. The hold can be
  cancelled by the customer, or expires once ``hold_expires_at`` has passed (lazily);
- open, payment in progress: ``capturing``, ``payment_pending``, ``needs_review``. The hold is
  kept; neither cancellation nor expiry applies;
- final: ``paid``, ``payment_failed``, ``cancelled``, ``expired``.

A cart has at most one open order (partial unique index), and a reservation belongs to at most
one order. Orders and order items are never updated except for the order's status and
timestamps, and never deleted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime
from app.domain.cart.models import MAX_QUANTITY_PER_LINE
from app.domain.catalog.vocab import LANGUAGES, sql_in

# Before capture: the customer may cancel, and the hold expires at hold_expires_at.
PRE_CAPTURE_STATUSES: tuple[str, ...] = ("pending_approval", "approved", "awaiting_payment")
# A payment may exist or be in flight: the hold is kept, never cancelled or expired.
PAYMENT_IN_PROGRESS_STATUSES: tuple[str, ...] = ("capturing", "payment_pending", "needs_review")
OPEN_STATUSES: tuple[str, ...] = PRE_CAPTURE_STATUSES + PAYMENT_IN_PROGRESS_STATUSES
FINAL_STATUSES: tuple[str, ...] = ("paid", "payment_failed", "cancelled", "expired")
ORDER_STATUSES: tuple[str, ...] = OPEN_STATUSES + FINAL_STATUSES

PUBLIC_REF_LENGTH = 32  # uuid4().hex
VERSION_HASH_LENGTH = 64  # SHA-256 hex digest

OPEN_ONLY = text(sql_in("status", OPEN_STATUSES))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_ref: Mapped[str] = mapped_column(String(PUBLIC_REF_LENGTH), unique=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="RESTRICT"))
    # The adopted reservation. Unique: a reservation is adopted by at most one order.
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id", ondelete="RESTRICT"), unique=True)
    status: Mapped[str] = mapped_column(String(24))
    version_hash: Mapped[str] = mapped_column(String(VERSION_HASH_LENGTH))
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_minor: Mapped[int] = mapped_column(Integer)
    shipping_minor: Mapped[int] = mapped_column(Integer)
    total_minor: Mapped[int] = mapped_column(Integer)
    ship_country: Mapped[str] = mapped_column(String(2))
    shipping_zone: Mapped[str] = mapped_column(String(32))
    # The customer's language when the order was created (presentation only; not in the hash).
    lang: Mapped[str] = mapped_column(String(8))
    hold_expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    status_changed_at: Mapped[datetime] = mapped_column(UTCDateTime())

    items: Mapped[list[OrderItem]] = relationship(back_populates="order", order_by="OrderItem.variant_id")

    __table_args__ = (
        CheckConstraint(sql_in("status", ORDER_STATUSES), name="status"),
        CheckConstraint(f"length(public_ref) = {PUBLIC_REF_LENGTH}", name="public_ref_length"),
        CheckConstraint(f"length(version_hash) = {VERSION_HASH_LENGTH}", name="version_hash_length"),
        CheckConstraint("currency = 'USD'", name="currency_usd"),
        CheckConstraint("subtotal_minor > 0", name="subtotal_positive"),
        CheckConstraint("shipping_minor >= 0", name="shipping_non_negative"),
        CheckConstraint("total_minor = subtotal_minor + shipping_minor", name="total_is_sum"),
        CheckConstraint("length(ship_country) = 2", name="ship_country_length"),
        CheckConstraint("ship_country = upper(ship_country)", name="ship_country_upper"),
        CheckConstraint(sql_in("lang", LANGUAGES), name="lang"),
        CheckConstraint("hold_expires_at > created_at", name="hold_expires_after_creation"),
        # At most one open order per cart. Partial indexes work on both SQLite and PostgreSQL.
        Index("uq_orders_cart_id_open", "cart_id", unique=True, sqlite_where=OPEN_ONLY, postgresql_where=OPEN_ONLY),
        Index("ix_orders_status_hold_expires_at", "status", "hold_expires_at"),
    )


class OrderItem(Base):
    """One line of the order as the customer reviewed it. Written once, never updated."""

    __tablename__ = "order_items"

    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"), primary_key=True)
    sku: Mapped[str] = mapped_column(String(64))
    product_slug: Mapped[str] = mapped_column(String(80))
    # The English catalogue name at creation. Responses show the live name in the requested
    # language; the snapshot keeps a stable record (and is not part of the hash, ADR-008).
    name_snapshot: Mapped[str] = mapped_column(String(120))
    size: Mapped[str] = mapped_column(String(16))
    colour: Mapped[str] = mapped_column(String(24))
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    line_total_minor: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("unit_price_minor > 0", name="unit_price_positive"),
        CheckConstraint(f"quantity BETWEEN 1 AND {MAX_QUANTITY_PER_LINE}", name="quantity_range"),
        CheckConstraint("line_total_minor = unit_price_minor * quantity", name="line_total_is_product"),
        Index("ix_order_items_variant_id", "variant_id"),
    )
