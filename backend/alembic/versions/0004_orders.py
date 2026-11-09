"""Orders that adopt the cart's reservation, and the sale movement (M3.1, ADR-025).

Additive except for three CHECK constraints that gain values:
- reservations: status gains ``order_hold`` (adopted by an order) and ``committed`` (sold, M3.2);
  ``ended_at_matches_status`` treats ``order_hold`` like ``active`` (not ended);
- inventory_movements: reason gains ``sale``; ``reason_deltas`` allows a sale to lower on-hand
  and reserved stock by the same amount; ``reservation_link`` requires a sale to name its
  reservation;
- new tables ``orders`` (one open order per cart, partial unique index; one order per
  reservation) and ``order_items`` (immutable snapshot).

No existing row is changed: every current reservation status and movement reason is still
accepted. The CHECK changes use Alembic batch mode with frozen table definitions
(``copy_from``), so on SQLite each table is rebuilt from a known definition (all rows copied,
indexes recreated, including the partial unique index on reservations). Rebuilding
``reservations`` drops a table that other tables reference, which SQLite allows only with
foreign keys off: ``alembic/env.py`` turns them off for SQLite migrations and runs
``PRAGMA foreign_key_check`` before committing. On PostgreSQL the constraints are altered in
place, inside the migration's transaction.

Downgrade (development and CI only; production is forward-only, ARCHITECTURE §7) refuses to run
if any order, adopted or committed reservation, or sale movement exists, because that would
delete order or sales history or leave values the old CHECKs reject. Otherwise it drops the two
tables and restores the 0003 constraints.

Revision ID: 0004_orders
Revises: 0003_cart_reservations
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_orders"
down_revision: str | Sequence[str] | None = "0003_cart_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen vocabularies. A migration must not change meaning when application code changes.
STOCK_ADJUSTMENT_REASONS = ("initial_stock", "restock", "adjustment")
RESERVATION_REASONS = ("reservation_hold", "reservation_release", "reservation_expiry")
SALE_REASONS = ("sale",)
M2_RESERVATION_STATUSES = ("active", "released", "expired")
M3_RESERVATION_STATUSES = ("active", "order_hold", "committed", "released", "expired")
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
LANGUAGES = ("en", "es", "fr", "de", "pt-BR")
MAX_QUANTITY_PER_LINE = 10

ACTIVE_ONLY = sa.text("status = 'active'")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


OPEN_ONLY = sa.text(_in("status", OPEN_ORDER_STATUSES))

M2_ENDED_AT = "(status = 'active' AND ended_at IS NULL) OR (status <> 'active' AND ended_at IS NOT NULL)"
M3_ENDED_AT = (
    "(status IN ('active', 'order_hold') AND ended_at IS NULL)"
    " OR (status NOT IN ('active', 'order_hold') AND ended_at IS NOT NULL)"
)
M2_REASON_DELTAS = (
    f"({_in('reason', RESERVATION_REASONS)} AND delta_on_hand = 0 AND delta_reserved <> 0)"
    f" OR ({_in('reason', STOCK_ADJUSTMENT_REASONS)} AND delta_reserved = 0)"
)
M3_REASON_DELTAS = (
    M2_REASON_DELTAS + f" OR ({_in('reason', SALE_REASONS)} AND delta_on_hand < 0 AND delta_reserved = delta_on_hand)"
)
M2_RESERVATION_LINK = (
    f"({_in('reason', RESERVATION_REASONS)} AND reservation_id IS NOT NULL)"
    f" OR ({_in('reason', STOCK_ADJUSTMENT_REASONS)} AND reservation_id IS NULL)"
)
M3_RESERVATION_LINK = (
    f"({_in('reason', RESERVATION_REASONS + SALE_REASONS)} AND reservation_id IS NOT NULL)"
    f" OR ({_in('reason', STOCK_ADJUSTMENT_REASONS)} AND reservation_id IS NULL)"
)


def _reservations_table(*, m3: bool) -> sa.Table:
    """reservations exactly as 0003 created it (m3=False) or as this revision leaves it."""
    metadata = sa.MetaData()
    sa.Table("carts", metadata, sa.Column("id", sa.Integer(), primary_key=True))  # FK target only
    statuses = M3_RESERVATION_STATUSES if m3 else M2_RESERVATION_STATUSES
    return sa.Table(
        "reservations",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("cart_id", sa.Integer(), nullable=False),
        sa.Column("cart_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reservations"),
        sa.UniqueConstraint("public_id", name="uq_reservations_public_id"),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], name="fk_reservations_cart_id_carts", ondelete="RESTRICT"),
        sa.CheckConstraint(_in("status", statuses), name="ck_reservations_status"),
        sa.CheckConstraint("length(public_id) = 32", name="ck_reservations_public_id_length"),
        sa.CheckConstraint("cart_version >= 0", name="ck_reservations_cart_version_non_negative"),
        sa.CheckConstraint("expires_at > created_at", name="ck_reservations_expires_after_creation"),
        sa.CheckConstraint(M3_ENDED_AT if m3 else M2_ENDED_AT, name="ck_reservations_ended_at_matches_status"),
        sa.Index(
            "uq_reservations_cart_id_active",
            "cart_id",
            unique=True,
            sqlite_where=ACTIVE_ONLY,
            postgresql_where=ACTIVE_ONLY,
        ),
        sa.Index("ix_reservations_status_expires_at", "status", "expires_at"),
    )


def _movements_table(*, m3: bool) -> sa.Table:
    """inventory_movements exactly as 0003 left it (m3=False) or as this revision leaves it."""
    metadata = sa.MetaData()
    # FK targets only, so the frozen definition can be compiled; never created here.
    sa.Table("variants", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("reservations", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    reasons = STOCK_ADJUSTMENT_REASONS + RESERVATION_REASONS + (SALE_REASONS if m3 else ())
    return sa.Table(
        "inventory_movements",
        metadata,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("delta_on_hand", sa.Integer(), nullable=False),
        sa.Column("delta_reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("on_hand_after", sa.Integer(), nullable=False),
        sa.Column("reserved_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("ref", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_movements"),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["variants.id"], name="fk_inventory_movements_variant_id_variants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name="fk_inventory_movements_reservation_id_reservations",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_in("reason", reasons), name="ck_inventory_movements_reason"),
        sa.CheckConstraint("delta_on_hand <> 0 OR delta_reserved <> 0", name="ck_inventory_movements_non_zero_delta"),
        sa.CheckConstraint("on_hand_after >= 0", name="ck_inventory_movements_on_hand_after_non_negative"),
        sa.CheckConstraint("reserved_after >= 0", name="ck_inventory_movements_reserved_after_non_negative"),
        sa.CheckConstraint(
            "reserved_after <= on_hand_after", name="ck_inventory_movements_available_after_non_negative"
        ),
        sa.CheckConstraint(
            M3_REASON_DELTAS if m3 else M2_REASON_DELTAS, name="ck_inventory_movements_reason_deltas"
        ),
        sa.CheckConstraint(
            M3_RESERVATION_LINK if m3 else M2_RESERVATION_LINK, name="ck_inventory_movements_reservation_link"
        ),
        sa.Index("ix_inventory_movements_variant_id_created_at", "variant_id", "created_at"),
        sa.Index("ix_inventory_movements_reservation_id", "reservation_id"),
    )


def _replace_checks(table: str, copy_from: sa.Table, checks: dict[str, str]) -> None:
    with op.batch_alter_table(table, copy_from=copy_from) as batch:
        for name, condition in checks.items():
            batch.drop_constraint(op.f(name), type_="check")
            batch.create_check_constraint(op.f(name), condition)


def upgrade() -> None:
    _replace_checks(
        "reservations",
        _reservations_table(m3=False),
        {
            "ck_reservations_status": _in("status", M3_RESERVATION_STATUSES),
            "ck_reservations_ended_at_matches_status": M3_ENDED_AT,
        },
    )
    _replace_checks(
        "inventory_movements",
        _movements_table(m3=False),
        {
            "ck_inventory_movements_reason": _in(
                "reason", STOCK_ADJUSTMENT_REASONS + RESERVATION_REASONS + SALE_REASONS
            ),
            "ck_inventory_movements_reason_deltas": M3_REASON_DELTAS,
            "ck_inventory_movements_reservation_link": M3_RESERVATION_LINK,
        },
    )

    op.create_table(
        "orders",
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
        sa.CheckConstraint(_in("status", ORDER_STATUSES), name=op.f("ck_orders_status")),
        sa.CheckConstraint("length(public_ref) = 32", name=op.f("ck_orders_public_ref_length")),
        sa.CheckConstraint("length(version_hash) = 64", name=op.f("ck_orders_version_hash_length")),
        sa.CheckConstraint("currency = 'USD'", name=op.f("ck_orders_currency_usd")),
        sa.CheckConstraint("subtotal_minor > 0", name=op.f("ck_orders_subtotal_positive")),
        sa.CheckConstraint("shipping_minor >= 0", name=op.f("ck_orders_shipping_non_negative")),
        sa.CheckConstraint("total_minor = subtotal_minor + shipping_minor", name=op.f("ck_orders_total_is_sum")),
        sa.CheckConstraint("length(ship_country) = 2", name=op.f("ck_orders_ship_country_length")),
        sa.CheckConstraint("ship_country = upper(ship_country)", name=op.f("ck_orders_ship_country_upper")),
        sa.CheckConstraint(_in("lang", LANGUAGES), name=op.f("ck_orders_lang")),
        sa.CheckConstraint("hold_expires_at > created_at", name=op.f("ck_orders_hold_expires_after_creation")),
    )
    # At most one open order per cart. Partial indexes work on both SQLite and PostgreSQL.
    op.create_index(
        "uq_orders_cart_id_open", "orders", ["cart_id"], unique=True, sqlite_where=OPEN_ONLY, postgresql_where=OPEN_ONLY
    )
    op.create_index("ix_orders_status_hold_expires_at", "orders", ["status", "hold_expires_at"])

    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("product_slug", sa.String(80), nullable=False),
        sa.Column("name_snapshot", sa.String(120), nullable=False),
        sa.Column("size", sa.String(16), nullable=False),
        sa.Column("colour", sa.String(24), nullable=False),
        sa.Column("unit_price_minor", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total_minor", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("order_id", "variant_id", name="pk_order_items"),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name="fk_order_items_order_id_orders", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["variants.id"], name="fk_order_items_variant_id_variants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("unit_price_minor > 0", name=op.f("ck_order_items_unit_price_positive")),
        sa.CheckConstraint(f"quantity BETWEEN 1 AND {MAX_QUANTITY_PER_LINE}", name=op.f("ck_order_items_quantity_range")),
        sa.CheckConstraint(
            "line_total_minor = unit_price_minor * quantity", name=op.f("ck_order_items_line_total_is_product")
        ),
    )
    op.create_index("ix_order_items_variant_id", "order_items", ["variant_id"])


def _refuse_if_order_history() -> None:
    connection = op.get_bind()
    orders = connection.execute(sa.text("SELECT COUNT(*) FROM orders")).scalar_one()
    adopted = connection.execute(
        sa.text("SELECT COUNT(*) FROM reservations WHERE status IN ('order_hold', 'committed')")
    ).scalar_one()
    sales = connection.execute(sa.text("SELECT COUNT(*) FROM inventory_movements WHERE reason = 'sale'")).scalar_one()
    if orders or adopted or sales:
        raise RuntimeError(
            "Refusing to downgrade 0004_orders: "
            f"{orders} order(s), {adopted} adopted or committed reservation(s) and {sales} sale movement(s) exist. "
            "Downgrading would delete order and sales history."
        )


def downgrade() -> None:
    # Development and CI only. Production is forward-only (ARCHITECTURE §7).
    _refuse_if_order_history()

    op.drop_index("ix_order_items_variant_id", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_status_hold_expires_at", table_name="orders")
    op.drop_index("uq_orders_cart_id_open", table_name="orders")
    op.drop_table("orders")

    _replace_checks(
        "inventory_movements",
        _movements_table(m3=True),
        {
            "ck_inventory_movements_reason": _in("reason", STOCK_ADJUSTMENT_REASONS + RESERVATION_REASONS),
            "ck_inventory_movements_reason_deltas": M2_REASON_DELTAS,
            "ck_inventory_movements_reservation_link": M2_RESERVATION_LINK,
        },
    )
    _replace_checks(
        "reservations",
        _reservations_table(m3=True),
        {
            "ck_reservations_status": _in("status", M2_RESERVATION_STATUSES),
            "ck_reservations_ended_at_matches_status": M2_ENDED_AT,
        },
    )
