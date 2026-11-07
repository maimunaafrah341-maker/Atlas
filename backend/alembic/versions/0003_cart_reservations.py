"""Carts and stock reservations (M2.1).

Additive: creates carts, cart_items, reservations and reservation_items, and extends
inventory_movements for reservation movements:
- three new reasons: reservation_hold, reservation_release, reservation_expiry;
- a nullable reservation_id (FK to reservations, RESTRICT), with an index;
- CHECK reason_deltas: reservation reasons move only reserved stock, stock adjustments only
  on-hand stock;
- CHECK reservation_link: reservation_id is set exactly for reservation reasons.

No existing row is changed. Every current movement is an M1 stock adjustment with
delta_reserved = 0, and the new column starts NULL, so both new CHECKs accept it.

The inventory_movements change uses Alembic batch mode with frozen table definitions
(``copy_from``), so on SQLite the table is rebuilt from a known definition rather than from
reflection, and all rows are copied. On PostgreSQL the column, constraints and index are
altered in place, inside the migration's transaction.

Downgrade (development and CI only; production is forward-only, ARCHITECTURE §7) refuses
to run if any reservation or reservation movement exists, because that would delete
reservation audit history. Otherwise it removes the movement index, FK, CHECKs and column,
restores the M1 reason list, then drops the four tables (including any carts).

Revision ID: 0003_cart_reservations
Revises: 0002_catalog_inventory_shipping
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_cart_reservations"
down_revision: str | Sequence[str] | None = "0002_catalog_inventory_shipping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen vocabularies. A migration must not change meaning when application code changes.
STOCK_ADJUSTMENT_REASONS = ("initial_stock", "restock", "adjustment")
RESERVATION_REASONS = ("reservation_hold", "reservation_release", "reservation_expiry")
RESERVATION_STATUSES = ("active", "released", "expired")
MAX_QUANTITY_PER_LINE = 10

ACTIVE_ONLY = sa.text("status = 'active'")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


REASON_DELTAS = (
    f"({_in('reason', RESERVATION_REASONS)} AND delta_on_hand = 0 AND delta_reserved <> 0)"
    f" OR ({_in('reason', STOCK_ADJUSTMENT_REASONS)} AND delta_reserved = 0)"
)
RESERVATION_LINK = (
    f"({_in('reason', RESERVATION_REASONS)} AND reservation_id IS NOT NULL)"
    f" OR ({_in('reason', STOCK_ADJUSTMENT_REASONS)} AND reservation_id IS NULL)"
)


def _movements_table(*, m2: bool) -> sa.Table:
    """inventory_movements exactly as 0002 created it (m2=False) or as this revision leaves it."""
    metadata = sa.MetaData()
    # FK targets only, so the frozen definition can be compiled; never created here.
    sa.Table("variants", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("reservations", metadata, sa.Column("id", sa.Integer(), primary_key=True))

    reasons = STOCK_ADJUSTMENT_REASONS + RESERVATION_REASONS if m2 else STOCK_ADJUSTMENT_REASONS
    elements: list[sa.SchemaItem] = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("delta_on_hand", sa.Integer(), nullable=False),
        sa.Column("delta_reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("on_hand_after", sa.Integer(), nullable=False),
        sa.Column("reserved_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
    ]
    if m2:
        elements.append(sa.Column("reservation_id", sa.Integer(), nullable=True))
    elements += [
        sa.Column("ref", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_movements"),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["variants.id"], name="fk_inventory_movements_variant_id_variants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(_in("reason", reasons), name="ck_inventory_movements_reason"),
        sa.CheckConstraint("delta_on_hand <> 0 OR delta_reserved <> 0", name="ck_inventory_movements_non_zero_delta"),
        sa.CheckConstraint("on_hand_after >= 0", name="ck_inventory_movements_on_hand_after_non_negative"),
        sa.CheckConstraint("reserved_after >= 0", name="ck_inventory_movements_reserved_after_non_negative"),
        sa.CheckConstraint(
            "reserved_after <= on_hand_after", name="ck_inventory_movements_available_after_non_negative"
        ),
        sa.Index("ix_inventory_movements_variant_id_created_at", "variant_id", "created_at"),
    ]
    if m2:
        elements += [
            sa.ForeignKeyConstraint(
                ["reservation_id"],
                ["reservations.id"],
                name="fk_inventory_movements_reservation_id_reservations",
                ondelete="RESTRICT",
            ),
            sa.CheckConstraint(REASON_DELTAS, name="ck_inventory_movements_reason_deltas"),
            sa.CheckConstraint(RESERVATION_LINK, name="ck_inventory_movements_reservation_link"),
            sa.Index("ix_inventory_movements_reservation_id", "reservation_id"),
        ]
    return sa.Table("inventory_movements", metadata, *elements)


def upgrade() -> None:
    op.create_table(
        "carts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_carts"),
        sa.UniqueConstraint("token_hash", name="uq_carts_token_hash"),
        sa.CheckConstraint("length(token_hash) = 64", name=op.f("ck_carts_token_hash_length")),
        sa.CheckConstraint("version >= 0", name=op.f("ck_carts_version_non_negative")),
        sa.CheckConstraint("expires_at > updated_at", name=op.f("ck_carts_expires_after_update")),
    )
    op.create_index("ix_carts_expires_at", "carts", ["expires_at"])

    op.create_table(
        "cart_items",
        sa.Column("cart_id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cart_id", "variant_id", name="pk_cart_items"),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], name="fk_cart_items_cart_id_carts", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["variants.id"], name="fk_cart_items_variant_id_variants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_QUANTITY_PER_LINE}", name=op.f("ck_cart_items_quantity_range")
        ),
    )
    op.create_index("ix_cart_items_variant_id", "cart_items", ["variant_id"])

    op.create_table(
        "reservations",
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
        sa.ForeignKeyConstraint(
            ["cart_id"], ["carts.id"], name="fk_reservations_cart_id_carts", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(_in("status", RESERVATION_STATUSES), name=op.f("ck_reservations_status")),
        sa.CheckConstraint("length(public_id) = 32", name=op.f("ck_reservations_public_id_length")),
        sa.CheckConstraint("cart_version >= 0", name=op.f("ck_reservations_cart_version_non_negative")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_reservations_expires_after_creation")),
        sa.CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) OR (status <> 'active' AND ended_at IS NOT NULL)",
            name=op.f("ck_reservations_ended_at_matches_status"),
        ),
    )
    # At most one active reservation per cart. Partial indexes work on both SQLite and PostgreSQL.
    op.create_index(
        "uq_reservations_cart_id_active",
        "reservations",
        ["cart_id"],
        unique=True,
        sqlite_where=ACTIVE_ONLY,
        postgresql_where=ACTIVE_ONLY,
    )
    op.create_index("ix_reservations_status_expires_at", "reservations", ["status", "expires_at"])

    op.create_table(
        "reservation_items",
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("reservation_id", "variant_id", name="pk_reservation_items"),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name="fk_reservation_items_reservation_id_reservations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["variants.id"], name="fk_reservation_items_variant_id_variants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            f"quantity BETWEEN 1 AND {MAX_QUANTITY_PER_LINE}", name=op.f("ck_reservation_items_quantity_range")
        ),
    )
    op.create_index("ix_reservation_items_variant_id", "reservation_items", ["variant_id"])

    with op.batch_alter_table("inventory_movements", copy_from=_movements_table(m2=False)) as batch:
        batch.add_column(sa.Column("reservation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_inventory_movements_reservation_id_reservations"),
            "reservations",
            ["reservation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint(op.f("ck_inventory_movements_reason"), type_="check")
        batch.create_check_constraint(
            op.f("ck_inventory_movements_reason"),
            _in("reason", STOCK_ADJUSTMENT_REASONS + RESERVATION_REASONS),
        )
        batch.create_check_constraint(op.f("ck_inventory_movements_reason_deltas"), REASON_DELTAS)
        batch.create_check_constraint(op.f("ck_inventory_movements_reservation_link"), RESERVATION_LINK)
        batch.create_index("ix_inventory_movements_reservation_id", ["reservation_id"])


def _refuse_if_reservation_history() -> None:
    connection = op.get_bind()
    reservations = connection.execute(sa.text("SELECT COUNT(*) FROM reservations")).scalar_one()
    movements = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM inventory_movements "
            f"WHERE reservation_id IS NOT NULL OR {_in('reason', RESERVATION_REASONS)}"
        )
    ).scalar_one()
    if reservations or movements:
        raise RuntimeError(
            "Refusing to downgrade 0003_cart_reservations: "
            f"{reservations} reservation(s) and {movements} reservation movement(s) exist. "
            "Downgrading would delete reservation audit history."
        )


def downgrade() -> None:
    # Development and CI only. Production is forward-only (ARCHITECTURE §7).
    _refuse_if_reservation_history()

    # Movement link first: index, FK and reservation-specific CHECKs, then the column, so the
    # reservations table is no longer referenced when it is dropped below.
    with op.batch_alter_table("inventory_movements", copy_from=_movements_table(m2=True)) as batch:
        batch.drop_index("ix_inventory_movements_reservation_id")
        batch.drop_constraint(op.f("fk_inventory_movements_reservation_id_reservations"), type_="foreignkey")
        batch.drop_constraint(op.f("ck_inventory_movements_reservation_link"), type_="check")
        batch.drop_constraint(op.f("ck_inventory_movements_reason_deltas"), type_="check")
        batch.drop_constraint(op.f("ck_inventory_movements_reason"), type_="check")
        batch.create_check_constraint(op.f("ck_inventory_movements_reason"), _in("reason", STOCK_ADJUSTMENT_REASONS))
        batch.drop_column("reservation_id")

    op.drop_index("ix_reservation_items_variant_id", table_name="reservation_items")
    op.drop_table("reservation_items")
    op.drop_index("ix_reservations_status_expires_at", table_name="reservations")
    op.drop_index("uq_reservations_cart_id_active", table_name="reservations")
    op.drop_table("reservations")
    op.drop_index("ix_cart_items_variant_id", table_name="cart_items")
    op.drop_table("cart_items")
    op.drop_index("ix_carts_expires_at", table_name="carts")
    op.drop_table("carts")
