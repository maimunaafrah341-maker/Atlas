"""Payment state on orders, and payment evidence (M3.2, ADR-026).

Additive. ``orders`` gains nullable payment columns (PayPal order id, unique; the stable create
request id; capture attempt, request id and start time; last reconciliation; review reason) and
CHECKs over them. Every existing order is in an M3.1 status (``pending_approval``, ``cancelled``
or ``expired``) with no capture attempt, so each new CHECK accepts it as is. New table
``payments``: minimal evidence read back from PayPal's capture record, one row per capture and
status.

The ``orders`` change uses batch mode with the frozen 0004 definition (``copy_from``), so on
SQLite the table is rebuilt from a known definition, with its partial unique index on open
orders, and every row copied (``alembic/env.py`` runs each revision in one transaction with
foreign keys off and checks them before commit). On PostgreSQL the columns, constraints and
index are added in place.

Downgrade (development and CI only; production is forward-only) refuses while any payment row,
PayPal order id or capture attempt exists, because that would delete payment history.

Revision ID: 0005_payments
Revises: 0004_orders
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_payments"
down_revision: str | Sequence[str] | None = "0004_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen vocabularies.
ORDER_STATUSES = (
    "pending_approval",
    "approved",
    "awaiting_payment",
    "capturing",
    "payment_pending",
    "needs_review",
    "paid",
    "payment_failed",
    "cancelled",
    "expired",
)
OPEN_ORDER_STATUSES = ORDER_STATUSES[:6]
PAYPAL_ORDER_STATUSES = ("awaiting_payment", "capturing", "payment_pending", "paid", "payment_failed")
REVIEW_REASONS = (
    "amount_mismatch",
    "currency_mismatch",
    "reference_mismatch",
    "order_mismatch",
    "payee_mismatch",
    "unexpected_status",
    "unresolved_timeout",
    "capture_after_end",
    "paypal_order_conflict",
)
LANGUAGES = ("en", "es", "fr", "de", "pt-BR")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


OPEN_ONLY = sa.text(_in("status", OPEN_ORDER_STATUSES))
NEW_CHECKS = {
    "ck_orders_capture_attempt_non_negative": "capture_attempt >= 0",
    "ck_orders_capture_attempt_recorded": (
        "(capture_attempt = 0) = (capture_request_id IS NULL AND capture_started_at IS NULL)"
    ),
    "ck_orders_paypal_order_for_payment": f"NOT ({_in('status', PAYPAL_ORDER_STATUSES)}) OR paypal_order_id IS NOT NULL",
    "ck_orders_review_reason": f"review_reason IS NULL OR {_in('review_reason', REVIEW_REASONS)}",
    "ck_orders_needs_review_has_reason": "status <> 'needs_review' OR review_reason IS NOT NULL",
}
NEW_COLUMNS = (
    "paypal_order_id",
    "paypal_create_request_id",
    "approved_at",
    "capture_attempt",
    "capture_request_id",
    "capture_started_at",
    "last_reconciled_at",
    "review_reason",
)


def _orders(*, m32: bool) -> sa.Table:
    """orders exactly as 0004 created it (m32=False) or as this revision leaves it (SQLite rebuilds)."""
    metadata = sa.MetaData()
    sa.Table("carts", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("reservations", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    extra: list[sa.SchemaItem] = []
    if m32:
        extra = [
            sa.Column("paypal_order_id", sa.String(36), nullable=True),
            sa.Column("paypal_create_request_id", sa.String(64), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("capture_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("capture_request_id", sa.String(64), nullable=True),
            sa.Column("capture_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("review_reason", sa.String(32), nullable=True),
            sa.UniqueConstraint("paypal_order_id", name="uq_orders_paypal_order_id"),
            *(sa.CheckConstraint(condition, name=name) for name, condition in NEW_CHECKS.items()),
        ]
    return sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_ref", sa.String(32), nullable=False),
        sa.Column("cart_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("subtotal_minor", sa.Integer(), nullable=False),
        sa.Column("shipping_minor", sa.Integer(), nullable=False),
        sa.Column("total_minor", sa.Integer(), nullable=False),
        sa.Column("ship_country", sa.String(2), nullable=False),
        sa.Column("shipping_zone", sa.String(32), nullable=False),
        sa.Column("lang", sa.String(8), nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("public_ref", name="uq_orders_public_ref"),
        sa.UniqueConstraint("reservation_id", name="uq_orders_reservation_id"),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], name="fk_orders_cart_id_carts", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["reservations.id"], name="fk_orders_reservation_id_reservations", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(_in("status", ORDER_STATUSES), name="ck_orders_status"),
        sa.CheckConstraint("length(public_ref) = 32", name="ck_orders_public_ref_length"),
        sa.CheckConstraint("length(version_hash) = 64", name="ck_orders_version_hash_length"),
        sa.CheckConstraint("currency = 'USD'", name="ck_orders_currency_usd"),
        sa.CheckConstraint("subtotal_minor > 0", name="ck_orders_subtotal_positive"),
        sa.CheckConstraint("shipping_minor >= 0", name="ck_orders_shipping_non_negative"),
        sa.CheckConstraint("total_minor = subtotal_minor + shipping_minor", name="ck_orders_total_is_sum"),
        sa.CheckConstraint("length(ship_country) = 2", name="ck_orders_ship_country_length"),
        sa.CheckConstraint("ship_country = upper(ship_country)", name="ck_orders_ship_country_upper"),
        sa.CheckConstraint(_in("lang", LANGUAGES), name="ck_orders_lang"),
        sa.CheckConstraint("hold_expires_at > created_at", name="ck_orders_hold_expires_after_creation"),
        sa.Index("uq_orders_cart_id_open", "cart_id", unique=True, sqlite_where=OPEN_ONLY, postgresql_where=OPEN_ONLY),
        sa.Index("ix_orders_status_hold_expires_at", "status", "hold_expires_at"),
        *extra,
    )


def upgrade() -> None:
    with op.batch_alter_table("orders", copy_from=_orders(m32=False)) as batch:
        batch.add_column(sa.Column("paypal_order_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("paypal_create_request_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("capture_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False))
        batch.add_column(sa.Column("capture_request_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("capture_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("review_reason", sa.String(32), nullable=True))
        batch.create_unique_constraint("uq_orders_paypal_order_id", ["paypal_order_id"])
        for name, condition in NEW_CHECKS.items():
            batch.create_check_constraint(op.f(name), condition)

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("paypal_order_id", sa.String(36), nullable=False),
        sa.Column("capture_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_reason", sa.String(64), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("invoice_id", sa.String(127), nullable=True),
        sa.Column("related_order_id", sa.String(36), nullable=True),
        sa.Column("payee_merchant_id", sa.String(32), nullable=True),
        sa.Column("verification", sa.String(16), nullable=False),
        sa.Column("problem", sa.String(32), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("debug_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_payments_order_id_orders", ondelete="RESTRICT"),
        sa.UniqueConstraint("capture_id", "status", name="uq_payments_capture_id_status"),
        sa.CheckConstraint("verification IN ('verified', 'mismatch')", name=op.f("ck_payments_verification")),
        sa.CheckConstraint("source IN ('capture', 'reconcile')", name=op.f("ck_payments_source")),
        sa.CheckConstraint(
            "(verification = 'verified') = (problem IS NULL)", name=op.f("ck_payments_problem_matches_verification")
        ),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"])


def _refuse_if_payment_history() -> None:
    connection = op.get_bind()
    payments = connection.execute(sa.text("SELECT COUNT(*) FROM payments")).scalar_one()
    started = connection.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE paypal_order_id IS NOT NULL OR capture_attempt > 0")
    ).scalar_one()
    if payments or started:
        raise RuntimeError(
            "Refusing to downgrade 0005_payments: "
            f"{payments} payment(s) and {started} order(s) with PayPal history exist. "
            "Downgrading would delete payment history."
        )


def downgrade() -> None:
    # Development and CI only. Production is forward-only (ARCHITECTURE §7).
    _refuse_if_payment_history()
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")
    with op.batch_alter_table("orders", copy_from=_orders(m32=True)) as batch:
        for name in NEW_CHECKS:
            batch.drop_constraint(op.f(name), type_="check")
        batch.drop_constraint("uq_orders_paypal_order_id", type_="unique")
        for column in NEW_COLUMNS:
            batch.drop_column(column)
