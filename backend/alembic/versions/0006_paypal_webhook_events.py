"""PayPal webhook events: deduplication and audit (M3.3).

Additive: one new table, ``paypal_webhook_events``, and its order index. No existing table,
column, constraint or row is touched, so upgrading is the same on SQLite and PostgreSQL and needs
no batch rebuild.

One row per PayPal webhook event, unique on PayPal's event id, holding only what deduplication,
retries and an audit timeline need: event id and type, the matched order (nullable, RESTRICT,
like ``payments``: orders are never deleted), the outcome, the delivery count and three
timestamps. No raw body, signature, certificate URL, transmission headers, payer data or amounts.

Downgrade (development and CI only; production is forward-only) drops only this table and its
index, and refuses while any webhook event row exists, because that would delete webhook history.
Orders, payments, reservations and inventory are never modified by either direction.

Revision ID: 0006_paypal_webhook_events
Revises: 0005_payments
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_paypal_webhook_events"
down_revision: str | Sequence[str] | None = "0005_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen vocabulary (app.domain.payments.models.WEBHOOK_OUTCOMES at this revision).
WEBHOOK_OUTCOMES = (
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


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "paypal_webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("deliveries", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_paypal_webhook_events"),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_paypal_webhook_events_order_id_orders", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("event_id", name="uq_paypal_webhook_events_event_id"),
        sa.CheckConstraint("length(event_id) > 0", name=op.f("ck_paypal_webhook_events_event_id_not_empty")),
        sa.CheckConstraint("length(event_type) > 0", name=op.f("ck_paypal_webhook_events_event_type_not_empty")),
        sa.CheckConstraint("deliveries >= 1", name=op.f("ck_paypal_webhook_events_deliveries_positive")),
        sa.CheckConstraint(
            f"outcome IS NULL OR {_in('outcome', WEBHOOK_OUTCOMES)}", name=op.f("ck_paypal_webhook_events_outcome")
        ),
        sa.CheckConstraint(
            "processed_at IS NULL OR outcome IS NOT NULL",
            name=op.f("ck_paypal_webhook_events_processed_has_outcome"),
        ),
    )
    op.create_index("ix_paypal_webhook_events_order_id", "paypal_webhook_events", ["order_id"])


def _refuse_if_webhook_history() -> None:
    events = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM paypal_webhook_events")).scalar_one()
    if events:
        raise RuntimeError(
            f"Refusing to downgrade 0006_paypal_webhook_events: {events} webhook event(s) exist. "
            "Downgrading would delete webhook history."
        )


def downgrade() -> None:
    # Development and CI only. Production is forward-only (ARCHITECTURE §7).
    _refuse_if_webhook_history()
    op.drop_index("ix_paypal_webhook_events_order_id", table_name="paypal_webhook_events")
    op.drop_table("paypal_webhook_events")
