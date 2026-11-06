"""Idempotent, create-only seed for the demo catalogue and shipping zones (ADR-022).

    python -m app.seed            # create missing rows only (production-safe)
    python -m app.seed --update   # also refresh descriptive fields and prices (local use)

Safeguards:
- Both seed files are fully validated before any database connection is opened.
- It refuses to run unless the schema is at the migration head.
- Everything happens in ONE transaction: the catalogue is seeded completely or not at all.
- Rows are never deleted. Stock is never written to an existing variant. Starting stock is
  set once, when a variant is created, and recorded as an ``initial_stock`` movement.
- Differences between the seed files and the database are reported as drift and left alone
  (unless --update).
- Any failure exits non-zero, so a deploy fails visibly.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import BACKEND_DIR, get_settings
from app.db import create_db_engine
from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant
from app.domain.catalog.service import MAX_PRICE_MINOR
from app.domain.catalog.vocab import LANGUAGES, SKU_PATTERN, SLUG_PATTERN, Category, Colour, Language, Size, Tag
from app.domain.inventory.service import adjust_stock
from app.domain.shipping.models import ShippingRate, ShippingRateCountry
from app.logging_setup import configure_logging, redact_text
from app.migrations import current_revision, head_revision

logger = logging.getLogger("app.seed")

SEED_DIR = BACKEND_DIR / "seed"
CATALOG_FILE = SEED_DIR / "catalog_v1.json"
SHIPPING_FILE = SEED_DIR / "shipping_v1.json"
SEED_REF = "seed:catalog-v1"

_CURRENCY_MARKER = re.compile(
    r"[$€£¥₹]|\b(?:usd|eur|gbp|brl|inr|dollars?|d[oó]lares?|euros?|reais|rupees?)\b", re.IGNORECASE
)
_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|shop|store)\b", re.IGNORECASE)
_SKU_SHAPE = re.compile(r"\b[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){2,}\b")
_DIGITS = re.compile(r"\d+")

Text120 = Annotated[str, Field(min_length=1, max_length=120)]


class SeedError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedTranslation(_Strict):
    name: Text120
    description: Annotated[str, Field(min_length=1, max_length=2000)]


class SeedVariant(_Strict):
    sku: Annotated[str, Field(max_length=64, pattern=SKU_PATTERN)]
    size: Size
    colour: Colour
    price_minor: Annotated[StrictInt, Field(gt=0, le=MAX_PRICE_MINOR)]
    initial_stock: Annotated[StrictInt, Field(ge=0)]
    low_stock_threshold: Annotated[StrictInt, Field(ge=0)]


class SeedProduct(_Strict):
    slug: Annotated[str, Field(max_length=80, pattern=SLUG_PATTERN)]
    category: Category
    tags: Annotated[list[Tag], Field(min_length=1)]
    sort_order: StrictInt
    translations: dict[Language, SeedTranslation]
    variants: Annotated[list[SeedVariant], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> SeedProduct:
        missing = [lang for lang in LANGUAGES if lang not in self.translations]
        if missing:
            raise ValueError(f"{self.slug}: missing translations {missing}")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError(f"{self.slug}: duplicate tags")
        combos = [(variant.size, variant.colour) for variant in self.variants]
        if len(set(combos)) != len(combos):
            raise ValueError(f"{self.slug}: duplicate size/colour variants")
        return self


class CatalogSeed(_Strict):
    version: StrictInt
    brand: Text120
    notice: str
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    machine_translated_languages: list[Language]
    products: Annotated[list[SeedProduct], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> CatalogSeed:
        slugs = [product.slug for product in self.products]
        if len(set(slugs)) != len(slugs):
            raise ValueError("duplicate product slugs")
        skus = [variant.sku for product in self.products for variant in product.variants]
        if len(set(skus)) != len(skus):
            raise ValueError("duplicate SKUs")
        for product in self.products:
            for problem in translation_problems(product, set(skus)):
                raise ValueError(problem)
        return self


def translation_problems(product: SeedProduct, skus: set[str]) -> list[str]:
    """ADR-018 applied to authored text: no protected values, and numbers survive translation."""
    problems: list[str] = []
    english = product.translations["en"]
    expected_digits = sorted(_DIGITS.findall(f"{english.name} {english.description}"))
    for lang, translation in product.translations.items():
        text = f"{translation.name} {translation.description}"
        where = f"{product.slug} [{lang}]"
        if _CURRENCY_MARKER.search(text):
            problems.append(f"{where}: contains a currency amount or marker")
        if _URL.search(text):
            problems.append(f"{where}: contains a URL")
        if _SKU_SHAPE.search(text) or any(sku.lower() in text.lower() for sku in skus):
            problems.append(f"{where}: contains a SKU")
        if sorted(_DIGITS.findall(text)) != expected_digits:
            problems.append(f"{where}: numbers differ from the English text")
    return problems


class SeedZone(_Strict):
    zone_code: Annotated[str, Field(max_length=32, pattern=r"^[a-z]+(?:_[a-z]+)*$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    price_minor: Annotated[StrictInt, Field(ge=0, le=MAX_PRICE_MINOR)]
    countries: Annotated[list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]], Field(min_length=1)]


class ShippingSeed(_Strict):
    version: StrictInt
    origin_country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    notice: str
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    zones: Annotated[list[SeedZone], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> ShippingSeed:
        codes = [zone.zone_code for zone in self.zones]
        if len(set(codes)) != len(codes):
            raise ValueError("duplicate zone codes")
        countries = [country for zone in self.zones for country in zone.countries]
        duplicates = sorted({country for country in countries if countries.count(country) > 1})
        if duplicates:
            raise ValueError(f"countries in more than one zone: {duplicates}")
        if self.origin_country in countries:
            raise ValueError("the origin country must not be a destination in this demo")
        return self


def load_seeds(
    catalog_path: Path = CATALOG_FILE, shipping_path: Path = SHIPPING_FILE, *, store_currency: str = "USD"
) -> tuple[CatalogSeed, ShippingSeed]:
    try:
        catalog = CatalogSeed.model_validate_json(catalog_path.read_text(encoding="utf-8"))
        shipping = ShippingSeed.model_validate_json(shipping_path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise SeedError(f"seed files are invalid: {error.error_count()} problem(s)\n{error}") from None
    for name, currency in (("catalogue", catalog.currency), ("shipping", shipping.currency)):
        if currency != store_currency:
            raise SeedError(f"{name} seed currency {currency} does not match STORE_CURRENCY {store_currency}")
    return catalog, shipping


@dataclass
class SeedReport:
    created: Counter[str] = field(default_factory=Counter)
    updated: Counter[str] = field(default_factory=Counter)
    drift: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"created": dict(self.created), "updated": dict(self.updated), "drift": self.drift}


def _seed_product(
    session: Session,
    seed: SeedProduct,
    catalog: CatalogSeed,
    existing: Product | None,
    report: SeedReport,
    update: bool,
) -> None:
    product = existing
    if product is None:
        product = Product(slug=seed.slug, category=seed.category, status="active", sort_order=seed.sort_order)
        session.add(product)
        session.flush()
        report.created["products"] += 1
    elif (product.category, product.sort_order) != (seed.category, seed.sort_order):
        if update:
            product.category, product.sort_order = seed.category, seed.sort_order
            report.updated["products"] += 1
        else:
            report.drift.append(f"product {seed.slug}: category/sort_order differ")

    by_lang = {translation.lang: translation for translation in product.translations}
    for lang, text in seed.translations.items():
        machine = lang in catalog.machine_translated_languages
        translation = by_lang.get(lang)
        if translation is None:
            translation = ProductTranslation(product_id=product.id, lang=lang, machine_translated=machine)
            translation.set_text(text.name, text.description)
            session.add(translation)
            report.created["translations"] += 1
        elif (translation.name, translation.description, translation.machine_translated) != (
            text.name,
            text.description,
            machine,
        ):
            if update:
                translation.set_text(text.name, text.description)
                translation.machine_translated = machine
                report.updated["translations"] += 1
            else:
                report.drift.append(f"translation {seed.slug} [{lang}] differs")

    have_tags = {tag.tag for tag in product.tags}
    for tag in seed.tags:
        if tag not in have_tags:
            session.add(ProductTag(product_id=product.id, tag=tag))
            report.created["tags"] += 1
    extra = sorted(have_tags - set(seed.tags))
    if extra:
        report.drift.append(f"product {seed.slug}: extra tags {extra} (never deleted)")

    for seed_variant in seed.variants:
        _seed_variant(session, product, seed_variant, catalog.currency, report, update)


def _seed_variant(
    session: Session, product: Product, seed: SeedVariant, currency: str, report: SeedReport, update: bool
) -> None:
    variant = session.execute(select(Variant).where(Variant.sku == seed.sku)).scalar_one_or_none()
    if variant is None:
        session.add(
            Variant(
                product_id=product.id,
                sku=seed.sku,
                size=seed.size,
                colour=seed.colour,
                price_minor=seed.price_minor,
                currency=currency,
                stock_on_hand=0,
                stock_reserved=0,
                low_stock_threshold=seed.low_stock_threshold,
                status="active",
            )
        )
        session.flush()
        if seed.initial_stock > 0:
            adjust_stock(session, seed.sku, seed.initial_stock, "initial_stock", ref=SEED_REF)
            report.created["initial_stock_movements"] += 1
        report.created["variants"] += 1
        return

    if variant.product_id != product.id:
        raise SeedError(f"SKU {seed.sku} already belongs to a different product")
    if (variant.size, variant.colour) != (seed.size, seed.colour):
        report.drift.append(f"variant {seed.sku}: size/colour differ (never changed by the seed)")
    if (variant.price_minor, variant.currency, variant.low_stock_threshold) != (
        seed.price_minor,
        currency,
        seed.low_stock_threshold,
    ):
        if update:
            variant.price_minor = seed.price_minor
            variant.currency = currency
            variant.low_stock_threshold = seed.low_stock_threshold
            report.updated["variants"] += 1
        else:
            report.drift.append(f"variant {seed.sku}: price/threshold differ")
    # Stock is never touched for an existing variant.


def _seed_shipping(session: Session, shipping: ShippingSeed, report: SeedReport, update: bool) -> None:
    zones = {rate.zone_code: rate for rate in session.execute(select(ShippingRate)).scalars()}
    mapped = {row.country_code: row for row in session.execute(select(ShippingRateCountry)).scalars()}
    for seed_zone in shipping.zones:
        rate = zones.get(seed_zone.zone_code)
        if rate is None:
            rate = ShippingRate(
                zone_code=seed_zone.zone_code,
                name=seed_zone.name,
                price_minor=seed_zone.price_minor,
                currency=shipping.currency,
                status="active",
            )
            session.add(rate)
            session.flush()
            report.created["shipping_zones"] += 1
        elif (rate.name, rate.price_minor, rate.currency) != (seed_zone.name, seed_zone.price_minor, shipping.currency):
            if update:
                rate.name, rate.price_minor, rate.currency = seed_zone.name, seed_zone.price_minor, shipping.currency
                report.updated["shipping_zones"] += 1
            else:
                report.drift.append(f"shipping zone {seed_zone.zone_code}: name/price differ")
        for country in seed_zone.countries:
            row = mapped.get(country)
            if row is None:
                session.add(ShippingRateCountry(country_code=country, shipping_rate_id=rate.id))
                report.created["shipping_countries"] += 1
            elif row.shipping_rate_id != rate.id:
                if update:
                    row.shipping_rate_id = rate.id
                    report.updated["shipping_countries"] += 1
                else:
                    report.drift.append(f"country {country}: mapped to a different zone")


def apply_seed(session: Session, catalog: CatalogSeed, shipping: ShippingSeed, *, update: bool = False) -> SeedReport:
    """Apply both seeds inside the caller's transaction. Never deletes and never overwrites stock."""
    report = SeedReport()
    existing = {
        product.slug: product
        for product in session.execute(
            select(Product).options(selectinload(Product.translations), selectinload(Product.tags))
        ).scalars()
    }
    for seed_product in catalog.products:
        _seed_product(session, seed_product, catalog, existing.get(seed_product.slug), report, update)
    _seed_shipping(session, shipping, report, update)
    session.flush()
    return report


def seed_database(database_url: str, *, update: bool = False, store_currency: str = "USD") -> SeedReport:
    catalog, shipping = load_seeds(store_currency=store_currency)
    engine = create_db_engine(database_url)
    try:
        with engine.connect() as connection:
            revision = current_revision(connection)
        if revision != head_revision():
            raise SeedError(f"schema is at {revision!r}, not the migration head; run migrations first")
        with Session(engine) as session, session.begin():
            return apply_seed(session, catalog, shipping, update=update)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the demo catalogue and shipping zones (create-only).")
    parser.add_argument("--update", action="store_true", help="also refresh descriptive fields and prices")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        report = seed_database(settings.database_url, update=args.update, store_currency=settings.store_currency)
    except Exception as error:
        logger.error("catalogue seed failed; no changes were made", extra={"error_type": type(error).__name__})
        print(f"seed failed: {redact_text(str(error))}", file=sys.stderr)
        return 1
    logger.info("catalogue seed complete", extra={"report": json.loads(json.dumps(report.as_dict()))})
    if report.drift:
        logger.warning("seed drift detected (left unchanged)", extra={"drift_count": len(report.drift)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
