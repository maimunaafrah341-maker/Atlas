"""Append-only inventory audit trail (ARCHITECTURE §3).

Every stock change writes one row in the same transaction as the change. The service layer
offers no way to update or delete movements.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow
from app.domain.catalog.vocab import sql_in

# Changes to stock_on_hand (M1). Only these are accepted by inventory.adjust_stock.
STOCK_ADJUSTMENT_REASONS: tuple[str, ...] = ("initial_stock", "restock", "adjustment")
# Changes to stock_reserved (M2): a cart reservation holds stock, is released, or expires.
RESERVATION_REASONS: tuple[str, ...] = ("reservation_hold", "reservation_release", "reservation_expiry")
# M3 (ADR-025): a committed reservation's stock leaves the shop. On-hand and reserved stock both
# fall by the item quantity, so available stock is unchanged.
SALE_REASONS: tuple[str, ...] = ("sale",)
# Movements that name their reservation.
RESERVATION_LINKED_REASONS: tuple[str, ...] = RESERVATION_REASONS + SALE_REASONS
MOVEMENT_REASONS: tuple[str, ...] = STOCK_ADJUSTMENT_REASONS + RESERVATION_REASONS + SALE_REASONS


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    delta_on_hand: Mapped[int] = mapped_column(Integer)
    delta_reserved: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    on_hand_after: Mapped[int] = mapped_column(Integer)
    reserved_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(24))
    # Set exactly for reservation movements and sales (M2, M3; ADR-024, ADR-025); NULL for stock adjustments.
    # RESTRICT: a reservation with movements is audit history and is never deleted.
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id", ondelete="RESTRICT"))
    ref: Mapped[str | None] = mapped_column(String(64))
    # Merchant-facing note. Must never contain customer personal data.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(sql_in("reason", MOVEMENT_REASONS), name="reason"),
        # Stock adjustments move only on-hand stock; reservation movements move only reserved
        # stock; a sale removes the same quantity from both.
        CheckConstraint(
            f"({sql_in('reason', RESERVATION_REASONS)} AND delta_on_hand = 0 AND delta_reserved <> 0)"
            f" OR ({sql_in('reason', STOCK_ADJUSTMENT_REASONS)} AND delta_reserved = 0)"
            f" OR ({sql_in('reason', SALE_REASONS)} AND delta_on_hand < 0 AND delta_reserved = delta_on_hand)",
            name="reason_deltas",
        ),
        # Reservation movements and sales always name their reservation; stock adjustments never do.
        CheckConstraint(
            f"({sql_in('reason', RESERVATION_LINKED_REASONS)} AND reservation_id IS NOT NULL)"
            f" OR ({sql_in('reason', STOCK_ADJUSTMENT_REASONS)} AND reservation_id IS NULL)",
            name="reservation_link",
        ),
        CheckConstraint("delta_on_hand <> 0 OR delta_reserved <> 0", name="non_zero_delta"),
        CheckConstraint("on_hand_after >= 0", name="on_hand_after_non_negative"),
        CheckConstraint("reserved_after >= 0", name="reserved_after_non_negative"),
        CheckConstraint("reserved_after <= on_hand_after", name="available_after_non_negative"),
        Index("ix_inventory_movements_variant_id_created_at", "variant_id", "created_at"),
        Index("ix_inventory_movements_reservation_id", "reservation_id"),
    )
