"""Public, read-only catalogue endpoints (PRODUCT_SPEC F3). No AI is involved."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import SessionDep
from app.domain.catalog.schemas import ProductOut, ProductPage
from app.domain.catalog.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_PRICE_MINOR,
    SearchFilters,
    SortOrder,
    get_product,
    search_products,
)
from app.domain.catalog.vocab import DEFAULT_LANGUAGE, SLUG_PATTERN, Category, Colour, Language, Size, Tag

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

PriceParam = Annotated[int | None, Query(ge=0, le=MAX_PRICE_MINOR, description="USD cents")]


@router.get("/products", response_model=ProductPage)
def list_products(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200, description="Words that must all match")] = None,
    lang: Language = DEFAULT_LANGUAGE,
    category: Annotated[list[Category] | None, Query()] = None,
    tag: Annotated[list[Tag] | None, Query(description="All listed tags must match")] = None,
    size: Annotated[list[Size] | None, Query()] = None,
    colour: Annotated[list[Colour] | None, Query()] = None,
    min_price_minor: PriceParam = None,
    max_price_minor: PriceParam = None,
    in_stock: bool = False,
    sort: SortOrder | None = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ProductPage:
    filters = SearchFilters(
        query=q,
        lang=lang,
        categories=tuple(category or ()),
        tags=tuple(dict.fromkeys(tag or ())),
        sizes=tuple(size or ()),
        colours=tuple(colour or ()),
        min_price_minor=min_price_minor,
        max_price_minor=max_price_minor,
        in_stock=in_stock,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return search_products(session, filters)


@router.get("/products/{slug}", response_model=ProductOut)
def product_detail(
    session: SessionDep,
    slug: Annotated[str, Path(max_length=80, pattern=SLUG_PATTERN)],
    lang: Language = DEFAULT_LANGUAGE,
) -> ProductOut:
    return get_product(session, slug, lang)
