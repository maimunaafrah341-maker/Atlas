"""Deterministic catalogue search and product retrieval (PRODUCT_SPEC F3, ADR-021).

The backend owns every result. Filtering happens in SQL. Text matching uses word-prefix
LIKE on normalised text, with a portable CASE-based score, so SQLite and PostgreSQL return
the same products in the same order. Every ordering ends with product id as a tiebreak.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from functools import reduce
from typing import Any, Literal, get_args

from sqlalchemy import ColumnElement, Select, case, exists, func, literal, select
from sqlalchemy.orm import Session, selectinload

from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant
from app.domain.catalog.schemas import Money, PriceRange, ProductOut, ProductPage, VariantOut
from app.domain.catalog.text import query_terms
from app.domain.catalog.vocab import DEFAULT_LANGUAGE, SIZE_ORDER, Language
from app.domain.inventory.service import stock_status
from app.errors import AtlasError, InvalidRequest

SortOrder = Literal["featured", "relevance", "price_asc", "price_desc"]
SORT_ORDERS: tuple[str, ...] = get_args(SortOrder)
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 20
MAX_PRICE_MINOR = 10_000_000


class ProductNotFound(AtlasError):
    status_code = 404
    code = "product_not_found"

    def __init__(self, slug: str) -> None:
        super().__init__(f"No product {slug}.", details=[{"slug": slug}])


class CatalogIntegrityError(AtlasError):
    """A stored product breaks a catalogue rule (e.g. no English text). This is a bug, not a user error."""

    status_code = 500
    code = "catalog_integrity_error"


@dataclass(frozen=True)
class SearchFilters:
    query: str | None = None
    lang: Language = DEFAULT_LANGUAGE
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    colours: tuple[str, ...] = ()
    min_price_minor: int | None = None
    max_price_minor: int | None = None
    in_stock: bool = False
    sort: SortOrder | None = None
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class _Plan:
    terms: list[str]
    sort: SortOrder


def _plan(filters: SearchFilters) -> _Plan:
    terms: list[str] = []
    if filters.query is not None and filters.query.strip():
        terms = query_terms(filters.query)
        if not terms:
            raise InvalidRequest("q", "must contain a word of at least 2 letters or digits")
    if (
        filters.min_price_minor is not None
        and filters.max_price_minor is not None
        and filters.min_price_minor > filters.max_price_minor
    ):
        raise InvalidRequest("min_price_minor", "must not exceed max_price_minor")
    if not 1 <= filters.page_size <= MAX_PAGE_SIZE:
        raise InvalidRequest("page_size", f"must be between 1 and {MAX_PAGE_SIZE}")
    if filters.page < 1:
        raise InvalidRequest("page", "must be at least 1")
    sort: SortOrder = filters.sort or ("relevance" if terms else "featured")
    if sort == "relevance" and not terms:
        raise InvalidRequest("sort", "relevance requires q")
    return _Plan(terms=terms, sort=sort)


def _variant_conditions(filters: SearchFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Variant.status == "active"]
    if filters.sizes:
        conditions.append(Variant.size.in_(filters.sizes))
    if filters.colours:
        conditions.append(Variant.colour.in_(filters.colours))
    if filters.min_price_minor is not None:
        conditions.append(Variant.price_minor >= filters.min_price_minor)
    if filters.max_price_minor is not None:
        conditions.append(Variant.price_minor <= filters.max_price_minor)
    if filters.in_stock:
        conditions.append(Variant.stock_on_hand - Variant.stock_reserved > 0)
    return conditions


def variant_matches(variant: Variant, filters: SearchFilters) -> bool:
    """Python mirror of _variant_conditions, used to flag matching variants in responses."""
    return (
        variant.status == "active"
        and (not filters.sizes or variant.size in filters.sizes)
        and (not filters.colours or variant.colour in filters.colours)
        and (filters.min_price_minor is None or variant.price_minor >= filters.min_price_minor)
        and (filters.max_price_minor is None or variant.price_minor <= filters.max_price_minor)
        and (not filters.in_stock or variant.available > 0)
    )


def _search_languages(lang: str) -> tuple[str, ...]:
    # English is always searchable: it is the fallback text and the canonical search language.
    return (lang,) if lang == DEFAULT_LANGUAGE else (lang, DEFAULT_LANGUAGE)


def _text_match(column: Any, term: str, languages: tuple[str, ...]) -> ColumnElement[bool]:
    return (
        exists()
        .where(
            ProductTranslation.product_id == Product.id,
            ProductTranslation.lang.in_(languages),
            column.like(f"% {term}%"),
        )
        .correlate(Product)
    )


def _build_query(filters: SearchFilters, plan: _Plan) -> Select[Any]:
    variant_conditions = _variant_conditions(filters)
    has_matching_variant = exists().where(Variant.product_id == Product.id, *variant_conditions).correlate(Product)
    # The displayed price_range is computed from the same matching variants (variant_matches),
    # so price sorting always agrees with what the customer sees.
    min_price = (
        select(func.min(Variant.price_minor))
        .where(Variant.product_id == Product.id, *variant_conditions)
        .correlate(Product)
        .scalar_subquery()
    )
    max_price = (
        select(func.max(Variant.price_minor))
        .where(Variant.product_id == Product.id, *variant_conditions)
        .correlate(Product)
        .scalar_subquery()
    )
    languages = _search_languages(filters.lang)

    score: ColumnElement[int]
    if plan.terms:
        parts = [
            case(
                (_text_match(ProductTranslation.search_name, term, languages), 2),
                (_text_match(ProductTranslation.search_text, term, languages), 1),
                else_=0,
            )
            for term in plan.terms
        ]
        score = reduce(lambda left, right: left + right, parts)
    else:
        score = literal(0)

    query = select(Product.id, score.label("score"), min_price.label("min_price"), max_price.label("max_price")).where(
        Product.status == "active", has_matching_variant
    )
    if filters.categories:
        query = query.where(Product.category.in_(filters.categories))
    for tag in filters.tags:
        query = query.where(
            exists().where(ProductTag.product_id == Product.id, ProductTag.tag == tag).correlate(Product)
        )
    for term in plan.terms:
        query = query.where(_text_match(ProductTranslation.search_text, term, languages))
    return query


def _ordered(query: Select[Any], sort: SortOrder) -> Select[Any]:
    score = query.selected_columns.score
    tiebreak = (Product.sort_order, Product.id)
    if sort == "relevance":
        return query.order_by(score.desc(), *tiebreak)
    # Cheapest first by the lowest matching price; dearest first by the highest matching price.
    if sort == "price_asc":
        return query.order_by(query.selected_columns.min_price.asc(), *tiebreak)
    if sort == "price_desc":
        return query.order_by(query.selected_columns.max_price.desc(), *tiebreak)
    return query.order_by(*tiebreak)


def _pick_translation(product: Product, lang: str) -> tuple[ProductTranslation, bool]:
    by_lang = {translation.lang: translation for translation in product.translations}
    if lang in by_lang:
        return by_lang[lang], False
    if DEFAULT_LANGUAGE in by_lang:
        return by_lang[DEFAULT_LANGUAGE], True
    raise CatalogIntegrityError(f"Product {product.slug} has no English text.")


def _to_view(product: Product, filters: SearchFilters) -> ProductOut:
    translation, fallback = _pick_translation(product, filters.lang)
    active = sorted(
        (variant for variant in product.variants if variant.status == "active"),
        key=lambda variant: (SIZE_ORDER[variant.size], variant.colour, variant.sku),
    )
    if not active:
        raise ProductNotFound(product.slug)
    # price_range covers the variants that match the active filters (all active variants when
    # there are none). If stock changed between the search and this load so that nothing
    # matches any more, fall back to all active variants rather than failing the request.
    matching = [variant for variant in active if variant_matches(variant, filters)] or active
    cheapest = min(matching, key=lambda variant: variant.price_minor)
    dearest = max(matching, key=lambda variant: variant.price_minor)
    return ProductOut(
        slug=product.slug,
        category=product.category,
        tags=[tag.tag for tag in product.tags],
        name=translation.name,
        description=translation.description,
        lang=translation.lang,
        fallback=fallback,
        machine_translated=translation.machine_translated,
        price_range=PriceRange(
            min=Money(amount_minor=cheapest.price_minor, currency=cheapest.currency),
            max=Money(amount_minor=dearest.price_minor, currency=dearest.currency),
        ),
        variants=[
            VariantOut(
                sku=variant.sku,
                size=variant.size,
                colour=variant.colour,
                price=Money(amount_minor=variant.price_minor, currency=variant.currency),
                stock_status=stock_status(variant.available, variant.low_stock_threshold),
                matches_filters=variant_matches(variant, filters),
            )
            for variant in active
        ],
    )


def _load(session: Session, ids: list[int]) -> dict[int, Product]:
    if not ids:
        return {}
    products = session.execute(
        select(Product)
        .where(Product.id.in_(ids))
        .options(selectinload(Product.translations), selectinload(Product.tags), selectinload(Product.variants))
    ).scalars()
    return {product.id: product for product in products}


def search_products(session: Session, filters: SearchFilters) -> ProductPage:
    plan = _plan(filters)
    query = _build_query(filters, plan)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = session.execute(
        _ordered(query, plan.sort).offset((filters.page - 1) * filters.page_size).limit(filters.page_size)
    ).all()
    ids = [row.id for row in rows]
    products = _load(session, ids)
    return ProductPage(
        items=[_to_view(products[product_id], filters) for product_id in ids],
        page=filters.page,
        page_size=filters.page_size,
        total=total,
        total_pages=math.ceil(total / filters.page_size) if total else 0,
    )


def get_product(session: Session, slug: str, lang: Language = DEFAULT_LANGUAGE) -> ProductOut:
    product = session.execute(
        select(Product)
        .where(Product.slug == slug, Product.status == "active")
        .options(selectinload(Product.translations), selectinload(Product.tags), selectinload(Product.variants))
    ).scalar_one_or_none()
    if product is None:
        raise ProductNotFound(slug)
    return _to_view(product, SearchFilters(lang=lang))


@dataclass(frozen=True)
class ProductName:
    """A product's display name in a requested language, with the catalogue's fallback flags."""

    name: str
    lang: str
    fallback: bool
    machine_translated: bool


def product_names(session: Session, slugs: Collection[str], lang: Language) -> dict[str, ProductName]:
    """Names for the given product slugs in ``lang``, by the same rule as the catalogue.

    Archived products are included (a cart may still hold them). Read-only.
    """
    if not slugs:
        return {}
    products = session.execute(
        select(Product).where(Product.slug.in_(set(slugs))).options(selectinload(Product.translations))
    ).scalars()
    names: dict[str, ProductName] = {}
    for product in products:
        translation, fallback = _pick_translation(product, lang)
        names[product.slug] = ProductName(
            name=translation.name,
            lang=translation.lang,
            fallback=fallback,
            machine_translated=translation.machine_translated,
        )
    return names
