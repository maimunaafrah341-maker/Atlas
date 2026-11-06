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

# M1 reasons only. Reservation, release and sale reasons arrive with M2/M3 via a migration.
MOVEMENT_REASONS: tuple[str, ...] = ("initial_stock", "restock", "adjustment")


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    delta_on_hand: Mapped[int] = mapped_column(Integer)
    delta_reserved: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    on_hand_after: Mapped[int] = mapped_column(Integer)
    reserved_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(24))
    ref: Mapped[str | None] = mapped_column(String(64))
    # Merchant-facing note. Must never contain customer personal data.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(sql_in("reason", MOVEMENT_REASONS), name="reason"),
        CheckConstraint("delta_on_hand <> 0 OR delta_reserved <> 0", name="non_zero_delta"),
        CheckConstraint("on_hand_after >= 0", name="on_hand_after_non_negative"),
        CheckConstraint("reserved_after >= 0", name="reserved_after_non_negative"),
        CheckConstraint("reserved_after <= on_hand_after", name="available_after_non_negative"),
        Index("ix_inventory_movements_variant_id_created_at", "variant_id", "created_at"),
    )
