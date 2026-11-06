"""Catalogue, inventory and shipping tables (M1).

Purely additive: creates new tables only and does not touch 0001_baseline. Vocabulary
lists are frozen here on purpose; a migration must not change meaning when application
code changes later. Tests prove every application vocabulary value is accepted.

Revision ID: 0002_catalog_inventory_shipping
Revises: 0001_baseline
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_catalog_inventory_shipping"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = ("jackets", "base_layers", "trousers", "socks", "accessories")
TAGS = (
    "waterproof",
    "windproof",
    "insulated",
    "packable",
    "breathable",
    "quick_dry",
    "merino",
    "uv_protection",
    "hiking",
    "trekking",
    "trail_running",
    "camping",
    "travel",
    "winter",
)
SIZES = ("XS", "S", "S_M", "M", "L", "L_XL", "XL", "XXL", "ONE_SIZE")
COLOURS = ("black", "charcoal", "grey", "white", "navy", "blue", "red", "orange", "yellow", "green", "olive", "khaki")
LANGUAGES = ("en", "es", "fr", "de", "pt-BR")
PRODUCT_STATUSES = ("draft", "active", "archived")
VARIANT_STATUSES = ("active", "archived")
MOVEMENT_REASONS = ("initial_stock", "restock", "adjustment")
ZONE_STATUSES = ("active", "inactive")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("slug", name="uq_products_slug"),
        sa.CheckConstraint(_in("category", CATEGORIES), name=op.f("ck_products_category")),
        sa.CheckConstraint(_in("status", PRODUCT_STATUSES), name=op.f("ck_products_status")),
    )
    op.create_index("ix_products_status_category_sort_order", "products", ["status", "category", "sort_order"])

    op.create_table(
        "product_translations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("lang", sa.String(8), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("machine_translated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("search_name", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_product_translations"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_product_translations_product_id_products",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("product_id", "lang", name="uq_product_translations_product_id_lang"),
        sa.CheckConstraint(_in("lang", LANGUAGES), name=op.f("ck_product_translations_lang")),
    )
    op.create_index("ix_product_translations_lang", "product_translations", ["lang"])

    op.create_table(
        "product_tags",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("tag", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("product_id", "tag", name="pk_product_tags"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_product_tags_product_id_products", ondelete="CASCADE"
        ),
        sa.CheckConstraint(_in("tag", TAGS), name=op.f("ck_product_tags_tag")),
    )
    op.create_index("ix_product_tags_tag", "product_tags", ["tag"])

    op.create_table(
        "variants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("size", sa.String(16), nullable=False),
        sa.Column("colour", sa.String(24), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("stock_on_hand", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("stock_reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_variants"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_variants_product_id_products", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("sku", name="uq_variants_sku"),
        sa.UniqueConstraint("product_id", "size", "colour", name="uq_variants_product_id_size_colour"),
        sa.CheckConstraint(_in("size", SIZES), name=op.f("ck_variants_size")),
        sa.CheckConstraint(_in("colour", COLOURS), name=op.f("ck_variants_colour")),
        sa.CheckConstraint(_in("status", VARIANT_STATUSES), name=op.f("ck_variants_status")),
        sa.CheckConstraint("price_minor > 0", name=op.f("ck_variants_price_positive")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_variants_currency_code")),
        sa.CheckConstraint("stock_on_hand >= 0", name=op.f("ck_variants_stock_on_hand_non_negative")),
        sa.CheckConstraint("stock_reserved >= 0", name=op.f("ck_variants_stock_reserved_non_negative")),
        sa.CheckConstraint("stock_reserved <= stock_on_hand", name=op.f("ck_variants_available_non_negative")),
        sa.CheckConstraint("low_stock_threshold >= 0", name=op.f("ck_variants_low_stock_threshold_non_negative")),
    )
    op.create_index("ix_variants_product_id", "variants", ["product_id"])

    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("delta_on_hand", sa.Integer(), nullable=False),
        sa.Column("delta_reserved", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("on_hand_after", sa.Integer(), nullable=False),
        sa.Column("reserved_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("ref", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_movements"),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["variants.id"], name="fk_inventory_movements_variant_id_variants", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(_in("reason", MOVEMENT_REASONS), name=op.f("ck_inventory_movements_reason")),
        sa.CheckConstraint("delta_on_hand <> 0 OR delta_reserved <> 0", name=op.f("ck_inventory_movements_non_zero_delta")),
        sa.CheckConstraint("on_hand_after >= 0", name=op.f("ck_inventory_movements_on_hand_after_non_negative")),
        sa.CheckConstraint("reserved_after >= 0", name=op.f("ck_inventory_movements_reserved_after_non_negative")),
        sa.CheckConstraint(
            "reserved_after <= on_hand_after", name=op.f("ck_inventory_movements_available_after_non_negative")
        ),
    )
    op.create_index(
        "ix_inventory_movements_variant_id_created_at", "inventory_movements", ["variant_id", "created_at"]
    )

    op.create_table(
        "shipping_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("zone_code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_shipping_rates"),
        sa.UniqueConstraint("zone_code", name="uq_shipping_rates_zone_code"),
        sa.CheckConstraint("price_minor >= 0", name=op.f("ck_shipping_rates_price_non_negative")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_shipping_rates_currency_code")),
        sa.CheckConstraint(_in("status", ZONE_STATUSES), name=op.f("ck_shipping_rates_status")),
    )

    op.create_table(
        "shipping_rate_countries",
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("shipping_rate_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("country_code", name="pk_shipping_rate_countries"),
        sa.ForeignKeyConstraint(
            ["shipping_rate_id"],
            ["shipping_rates.id"],
            name="fk_shipping_rate_countries_shipping_rate_id_shipping_rates",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("length(country_code) = 2", name=op.f("ck_shipping_rate_countries_country_code_length")),
        sa.CheckConstraint("country_code = upper(country_code)", name=op.f("ck_shipping_rate_countries_country_code_upper")),
    )
    op.create_index(
        "ix_shipping_rate_countries_shipping_rate_id", "shipping_rate_countries", ["shipping_rate_id"]
    )


def downgrade() -> None:
    # Development and CI only. Production is forward-only (ARCHITECTURE §7).
    op.drop_index("ix_shipping_rate_countries_shipping_rate_id", table_name="shipping_rate_countries")
    op.drop_table("shipping_rate_countries")
    op.drop_table("shipping_rates")
    op.drop_index("ix_inventory_movements_variant_id_created_at", table_name="inventory_movements")
    op.drop_table("inventory_movements")
    op.drop_index("ix_variants_product_id", table_name="variants")
    op.drop_table("variants")
    op.drop_index("ix_product_tags_tag", table_name="product_tags")
    op.drop_table("product_tags")
    op.drop_index("ix_product_translations_lang", table_name="product_translations")
    op.drop_table("product_translations")
    op.drop_index("ix_products_status_category_sort_order", table_name="products")
    op.drop_table("products")
