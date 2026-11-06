"""Deterministic shipping-rate lookup (PRODUCT_SPEC F4).

The destination must be an explicit ISO 3166-1 alpha-2 code. If no active zone covers it,
the lookup fails with a typed error. It never invents a price and never assumes free
shipping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.shipping.models import ShippingRate, ShippingRateCountry
from app.errors import AtlasError

_COUNTRY = re.compile(r"^[A-Za-z]{2}$")


class InvalidCountryCode(AtlasError):
    status_code = 422
    code = "invalid_country_code"

    def __init__(self) -> None:
        super().__init__(
            "Country must be a two-letter ISO 3166-1 code.",
            details=[{"field": "country", "issue": "expected two letters, e.g. FR"}],
        )


class UnsupportedDestination(AtlasError):
    status_code = 422
    code = "unsupported_destination"

    def __init__(self, country: str) -> None:
        super().__init__(f"We do not ship to {country}.", details=[{"country": country}])


@dataclass(frozen=True)
class ShippingQuote:
    country: str
    zone_code: str
    zone_name: str
    amount_minor: int
    currency: str


def normalize_country(country: str) -> str:
    value = country.strip()
    if not _COUNTRY.match(value):
        raise InvalidCountryCode()
    return value.upper()


def quote(session: Session, country: str) -> ShippingQuote:
    code = normalize_country(country)
    rate = session.execute(
        select(ShippingRate)
        .join(ShippingRateCountry, ShippingRateCountry.shipping_rate_id == ShippingRate.id)
        .where(ShippingRateCountry.country_code == code, ShippingRate.status == "active")
    ).scalar_one_or_none()
    if rate is None:
        raise UnsupportedDestination(code)
    return ShippingQuote(
        country=code,
        zone_code=rate.zone_code,
        zone_name=rate.name,
        amount_minor=rate.price_minor,
        currency=rate.currency,
    )
