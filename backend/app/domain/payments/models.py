"""Payment evidence read back from PayPal (M3.2, ADR-026).

One row per observed state of one PayPal capture, written by the shared finalization service
from PayPal's own capture record (``GET /v2/payments/captures/{id}``), never from the browser.
Only what verification and reconciliation need is kept: no raw response bodies and no payer
data (no names, emails, addresses or payer ids). Rows are append-only and never deleted.

``verification`` records whether the capture matched the order snapshot (amount, USD, invoice
id = order reference, PayPal order id, merchant); ``problem`` names the first check that failed.
The same capture in the same status is recorded once (unique), so repeated reconciliation adds
nothing.

``PayPalWebhookEvent`` (M3.3): one row per PayPal webhook event, keyed by PayPal's event id
(unique), written only after PayPal's verification API confirmed the signature. A webhook is a
notification, never evidence: the payment state still comes from PayPal's API through the shared
reconciliation and ``finalize``. The row keeps only what deduplication, retries and an audit
timeline need: the event id and type, the matched order, how many times PayPal delivered it, the
outcome and three timestamps. No raw body, resource, signature, certificate URL, transmission
headers, payer data or amounts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
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


# What processing a webhook event led to: the payment service's result for the matched order, or
# ``ignored`` (an event type Atlas doesn't act on) / ``unmatched`` (no Atlas order).
WEBHOOK_OUTCOMES: tuple[str, ...] = (
    "paid",
    "pending",
    "processing",
    "instrument_declined",
    "payer_action_required",
    "failed",
    "needs_review",
    "awaiting_payment",
    "closed",
    "ignored",
    "unmatched",
)


class PayPalWebhookEvent(Base):
    __tablename__ = "paypal_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # PayPal's event id (``WH-...``): the deduplication key.
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))  # as PayPal named it; unknown types are kept
    # The Atlas order the event was matched to, if any. RESTRICT, like ``payments``: orders are
    # never deleted, and an order with webhook history must not lose (or silently orphan) it.
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    outcome: Mapped[str | None] = mapped_column(String(24))
    # Deliveries of this event that passed signature verification (PayPal retries, resends).
    deliveries: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())  # the first verified delivery
    last_received_at: Mapped[datetime] = mapped_column(UTCDateTime())  # the latest verified delivery
    # Set once processing reached a settled outcome; NULL means a redelivery processes it again.
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        CheckConstraint("length(event_id) > 0", name="event_id_not_empty"),
        CheckConstraint("length(event_type) > 0", name="event_type_not_empty"),
        CheckConstraint("deliveries >= 1", name="deliveries_positive"),
        CheckConstraint(f"outcome IS NULL OR {sql_in('outcome', WEBHOOK_OUTCOMES)}", name="outcome"),
        CheckConstraint("processed_at IS NULL OR outcome IS NOT NULL", name="processed_has_outcome"),
        Index("ix_paypal_webhook_events_order_id", "order_id"),
    )
