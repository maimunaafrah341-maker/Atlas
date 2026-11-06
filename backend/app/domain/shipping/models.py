"""Merchant-configured shipping zones with flat USD rates (PRODUCT_SPEC F4, D-1).

A country belongs to at most one zone: ``country_code`` is the primary key of
shipping_rate_countries. A country with no row is unsupported. There is no catch-all zone.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.domain.catalog.vocab import sql_in

ZONE_STATUSES: tuple[str, ...] = ("active", "inactive")


class ShippingRate(Base):
    __tablename__ = "shipping_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    # Zero is allowed only when a merchant explicitly configures free shipping for a zone.
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")

    countries: Mapped[list[ShippingRateCountry]] = relationship(
        back_populates="rate", cascade="all, delete-orphan", order_by="ShippingRateCountry.country_code"
    )

    __table_args__ = (
        CheckConstraint("price_minor >= 0", name="price_non_negative"),
        CheckConstraint("length(currency) = 3", name="currency_code"),
        CheckConstraint(sql_in("status", ZONE_STATUSES), name="status"),
    )


class ShippingRateCountry(Base):
    __tablename__ = "shipping_rate_countries"

    country_code: Mapped[str] = mapped_column(String(2), primary_key=True)
    shipping_rate_id: Mapped[int] = mapped_column(ForeignKey("shipping_rates.id", ondelete="CASCADE"))

    rate: Mapped[ShippingRate] = relationship(back_populates="countries")

    __table_args__ = (
        CheckConstraint("length(country_code) = 2", name="country_code_length"),
        CheckConstraint("country_code = upper(country_code)", name="country_code_upper"),
        Index("ix_shipping_rate_countries_shipping_rate_id", "shipping_rate_id"),
    )
