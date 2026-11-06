"""Catalogue tables: products, their translations and tags, and sellable variants.

Prices live only on variants, in integer minor units (USD cents); never on translations
(ARCHITECTURE §3, ADR-003). Stock fields live on variants. They change only through
app.domain.inventory, which records every change in inventory_movements.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow
from app.domain.catalog.text import search_document
from app.domain.catalog.vocab import (
    CATEGORIES,
    COLOURS,
    LANGUAGES,
    PRODUCT_STATUSES,
    SIZES,
    TAGS,
    VARIANT_STATUSES,
    sql_in,
)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    category: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    translations: Mapped[list[ProductTranslation]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductTranslation.lang"
    )
    tags: Mapped[list[ProductTag]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductTag.tag"
    )
    variants: Mapped[list[Variant]] = relationship(back_populates="product", order_by="Variant.sku")

    __table_args__ = (
        CheckConstraint(sql_in("category", CATEGORIES), name="category"),
        CheckConstraint(sql_in("status", PRODUCT_STATUSES), name="status"),
        Index("ix_products_status_category_sort_order", "status", "category", "sort_order"),
    )


class ProductTranslation(Base):
    __tablename__ = "product_translations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    # ADR-015: machine-produced text is flagged until a native speaker has reviewed it.
    machine_translated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Derived search fields (ADR-021). Always written through set_text(), never directly.
    search_name: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)

    product: Mapped[Product] = relationship(back_populates="translations")

    __table_args__ = (
        UniqueConstraint("product_id", "lang"),
        CheckConstraint(sql_in("lang", LANGUAGES), name="lang"),
        Index("ix_product_translations_lang", "lang"),
    )

    def set_text(self, name: str, description: str) -> None:
        self.name = name
        self.description = description
        self.search_name = search_document(name)
        self.search_text = search_document(name, description)

    def search_fields_consistent(self) -> bool:
        return self.search_name == search_document(self.name) and self.search_text == search_document(
            self.name, self.description
        )


class ProductTag(Base):
    __tablename__ = "product_tags"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(32), primary_key=True)

    product: Mapped[Product] = relationship(back_populates="tags")

    __table_args__ = (
        CheckConstraint(sql_in("tag", TAGS), name="tag"),
        Index("ix_product_tags_tag", "tag"),
    )


class Variant(Base):
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # RESTRICT: variants will be referenced by orders (M2+), so never cascade-delete them.
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    size: Mapped[str] = mapped_column(String(16))
    colour: Mapped[str] = mapped_column(String(24))
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    stock_on_hand: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    stock_reserved: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    product: Mapped[Product] = relationship(back_populates="variants")

    __table_args__ = (
        UniqueConstraint("product_id", "size", "colour"),
        CheckConstraint(sql_in("size", SIZES), name="size"),
        CheckConstraint(sql_in("colour", COLOURS), name="colour"),
        CheckConstraint(sql_in("status", VARIANT_STATUSES), name="status"),
        CheckConstraint("price_minor > 0", name="price_positive"),
        CheckConstraint("length(currency) = 3", name="currency_code"),
        CheckConstraint("stock_on_hand >= 0", name="stock_on_hand_non_negative"),
        CheckConstraint("stock_reserved >= 0", name="stock_reserved_non_negative"),
        # The inventory invariant: available = on_hand - reserved is never negative.
        CheckConstraint("stock_reserved <= stock_on_hand", name="available_non_negative"),
        CheckConstraint("low_stock_threshold >= 0", name="low_stock_threshold_non_negative"),
        Index("ix_variants_product_id", "product_id"),
    )

    @property
    def available(self) -> int:
        return self.stock_on_hand - self.stock_reserved
