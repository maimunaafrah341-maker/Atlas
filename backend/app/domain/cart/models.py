"""Persistent anonymous carts (M2, ADR-024).

A cart is identified by an opaque random token held in an httpOnly cookie. Only the
token's SHA-256 hex digest is stored. Cart lines hold a variant and a quantity, never a
price: prices, names and availability are always read live from the catalogue.

``version`` serialises cart changes and reservation requests (ADR-024): every cart edit and
every new reservation advances it with a compare-and-set on the cart row, and a reservation
records the version it was made for.

Limits (D4): at most MAX_QUANTITY_PER_LINE units per line (also a database CHECK) and at most
MAX_CART_LINES lines per cart (enforced by the cart service). A cart expires
CART_IDLE_LIFETIME after its last mutation (D8); ``expires_at`` is set on every mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime

MAX_QUANTITY_PER_LINE = 10
MAX_CART_LINES = 20
CART_IDLE_LIFETIME = timedelta(days=30)
TOKEN_HASH_LENGTH = 64  # SHA-256 hex digest


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", order_by="CartItem.created_at"
    )

    __table_args__ = (
        CheckConstraint(f"length(token_hash) = {TOKEN_HASH_LENGTH}", name="token_hash_length"),
        CheckConstraint("version >= 0", name="version_non_negative"),
        CheckConstraint("expires_at > updated_at", name="expires_after_update"),
        Index("ix_carts_expires_at", "expires_at"),
    )


class CartItem(Base):
    __tablename__ = "cart_items"

    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), primary_key=True)
    # RESTRICT: a variant in someone's cart is archived, never deleted.
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())

    cart: Mapped[Cart] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint(f"quantity BETWEEN 1 AND {MAX_QUANTITY_PER_LINE}", name="quantity_range"),
        Index("ix_cart_items_variant_id", "variant_id"),
    )
