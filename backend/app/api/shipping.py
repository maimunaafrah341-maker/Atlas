"""Public, read-only shipping quote endpoint (PRODUCT_SPEC F4)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.domain.catalog.schemas import Money
from app.domain.shipping.service import quote

router = APIRouter(prefix="/api/shipping", tags=["shipping"])


class ShippingQuoteOut(BaseModel):
    country: str
    zone: str
    zone_name: str
    price: Money


@router.get("/quote", response_model=ShippingQuoteOut)
def shipping_quote(
    session: SessionDep,
    country: Annotated[str, Query(min_length=1, max_length=8, description="ISO 3166-1 alpha-2, e.g. FR")],
) -> ShippingQuoteOut:
    result = quote(session, country)
    return ShippingQuoteOut(
        country=result.country,
        zone=result.zone_code,
        zone_name=result.zone_name,
        price=Money(amount_minor=result.amount_minor, currency=result.currency),
    )
