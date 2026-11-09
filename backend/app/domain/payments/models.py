"""Payment evidence read back from PayPal (M3.2, ADR-026).

One row per observed state of one PayPal capture, written by the shared finalization service
from PayPal's own capture record (``GET /v2/payments/captures/{id}``), never from the browser.
Only what verification and reconciliation need is kept: no raw response bodies and no payer
data (no names, emails, addresses or payer ids). Rows are append-only and never deleted.

``verification`` records whether the capture matched the order snapshot (amount, USD, invoice
id = order reference, PayPal order id, merchant); ``problem`` names the first check that failed.
The same capture in the same status is recorded once (unique), so repeated reconciliation adds
nothing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime
from app.domain.catalog.vocab import sql_in

VERIFICATIONS: tuple[str, ...] = ("verified", "mismatch")
SOURCES: tuple[str, ...] = ("capture", "reconcile")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    paypal_order_id: Mapped[str] = mapped_column(String(36))
    capture_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24))  # PayPal's capture status, as reported
    status_reason: Mapped[str | None] = mapped_column(String(64))
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))
    invoice_id: Mapped[str | None] = mapped_column(String(127))
    related_order_id: Mapped[str | None] = mapped_column(String(36))
    payee_merchant_id: Mapped[str | None] = mapped_column(String(32))
    verification: Mapped[str] = mapped_column(String(16))
    problem: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))
    debug_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())

    __table_args__ = (
        UniqueConstraint("capture_id", "status"),
        CheckConstraint(sql_in("verification", VERIFICATIONS), name="verification"),
        CheckConstraint(sql_in("source", SOURCES), name="source"),
        CheckConstraint("(verification = 'verified') = (problem IS NULL)", name="problem_matches_verification"),
        Index("ix_payments_order_id", "order_id"),
    )
