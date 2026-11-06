"""Public catalogue response schemas.

Money is always an integer of minor units plus an ISO currency code. The server never sends
formatted prices; clients format with Intl for the user's locale (ARCHITECTURE §8.3). Stock
is exposed only as a status, never as a count (D-5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictInt

from app.domain.catalog.vocab import Category, Colour, Language, Size, StockStatus, Tag


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount_minor: StrictInt
    currency: str


class PriceRange(BaseModel):
    min: Money
    max: Money


class VariantOut(BaseModel):
    sku: str
    size: Size
    colour: Colour
    price: Money
    stock_status: StockStatus
    matches_filters: bool


class ProductOut(BaseModel):
    slug: str
    category: Category
    tags: list[Tag]
    name: str
    description: str
    lang: Language
    fallback: bool
    machine_translated: bool
    price_range: PriceRange
    variants: list[VariantOut]


class ProductPage(BaseModel):
    items: list[ProductOut]
    page: int
    page_size: int
    total: int
    total_pages: int
