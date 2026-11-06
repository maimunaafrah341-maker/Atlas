"""Schema tests: migration matches models, constraints exist, vocabularies and invariants are enforced."""

from collections.abc import Callable

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db import Base
from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant
from app.domain.catalog.vocab import CATEGORIES, COLOURS, LANGUAGES, SIZES, TAGS
from app.domain.inventory.service import adjust_stock
from app.domain.shipping.models import ShippingRate, ShippingRateCountry
from app.migrations import head_revision
from tests.factories import V, make_product

M1_TABLES = {
    "products",
    "product_translations",
    "product_tags",
    "variants",
    "inventory_movements",
    "shipping_rates",
    "shipping_rate_countries",
}


def test_head_is_m1_migration() -> None:
    assert head_revision() == "0002_catalog_inventory_shipping"


def test_models_match_migrated_schema(db_session: Session) -> None:
    connection = db_session.connection()
    context = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
    assert compare_metadata(context, Base.metadata) == []


def test_check_constraint_names_match_models(db_session: Session) -> None:
    inspector = inspect(db_session.connection())
    assert set(inspector.get_table_names()) >= M1_TABLES
    for table in M1_TABLES:
        expected = {
            constraint.name
            for constraint in Base.metadata.tables[table].constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        reflected = {item["name"] for item in inspector.get_check_constraints(table)}
        assert reflected == expected, table


def test_every_vocabulary_value_is_accepted(db_session: Session) -> None:
    for index, category in enumerate(CATEGORIES):
        make_product(db_session, f"cat-{index}", category=category, variants=[])
    product = make_product(
        db_session,
        "all-values",
        tags=TAGS,
        translations={lang: (f"Name {lang}", "Description") for lang in LANGUAGES},
        variants=[V(sku=f"SZ-{index}", size=size, colour=COLOURS[index]) for index, size in enumerate(SIZES)],
    )
    db_session.add_all(
        Variant(
            product_id=product.id,
            sku=f"CL-{index}",
            size="XXL",
            colour=colour,
            price_minor=1,
            currency="USD",
        )
        for index, colour in enumerate(COLOURS)
        if index != SIZES.index("XXL")
    )
    db_session.commit()


def _rejects(session: Session, change: Callable[[Session], None]) -> None:
    def attempt() -> None:
        change(session)
        session.flush()

    with pytest.raises(IntegrityError):
        attempt()
    session.rollback()


def test_duplicate_sku_rejected(db_session: Session) -> None:
    make_product(db_session, "first", variants=[V(sku="DUP-1")])
    db_session.commit()
    _rejects(db_session, lambda s: make_product(s, "second", variants=[V(sku="DUP-1")]) and None)


def test_duplicate_translation_rejected(db_session: Session) -> None:
    product = make_product(db_session, "one")
    db_session.commit()

    def duplicate(session: Session) -> None:
        translation = ProductTranslation(product_id=product.id, lang="en")
        translation.set_text("Again", "Again")
        session.add(translation)

    _rejects(db_session, duplicate)


def test_duplicate_size_colour_rejected(db_session: Session) -> None:
    _rejects(
        db_session,
        lambda s: make_product(s, "twins", variants=[V(sku="T-1"), V(sku="T-2")]) and None,
    )


@pytest.mark.parametrize(
    "variant",
    [
        V(sku="BAD-1", on_hand=-1),
        V(sku="BAD-2", on_hand=2, reserved=3),  # available would be negative
        V(sku="BAD-3", reserved=-1),
        V(sku="BAD-4", price_minor=0),
        V(sku="BAD-5", threshold=-1),
        V(sku="BAD-6", size="XXXL"),
        V(sku="BAD-7", colour="purple"),
        V(sku="BAD-8", status="deleted"),
    ],
)
def test_invalid_variant_rejected(db_session: Session, variant: V) -> None:
    _rejects(db_session, lambda s: make_product(s, "invalid", variants=[variant]) and None)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.add(Product(slug="x", category="boots", status="active", sort_order=0)),
        lambda s: s.add(Product(slug="x", category="jackets", status="live", sort_order=0)),
        lambda s: make_product(s, "x", translations={"it": ("Giacca", "Una giacca.")}),
        lambda s: make_product(s, "x", tags=("fashionable",)),
    ],
)
def test_invalid_catalogue_codes_rejected(db_session: Session, change: Callable[[Session], object]) -> None:
    _rejects(db_session, lambda s: change(s) and None)


def test_variant_with_movements_cannot_be_deleted(db_session: Session) -> None:
    make_product(db_session, "audited", variants=[V(sku="AUD-1", on_hand=0)])
    adjust_stock(db_session, "AUD-1", 5, "restock")
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Variant).where(Variant.sku == "AUD-1"))
    db_session.rollback()


def test_product_with_variants_cannot_be_deleted(db_session: Session) -> None:
    make_product(db_session, "kept", variants=[V(sku="KEEP-1")])
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Product).where(Product.slug == "kept"))
    db_session.rollback()


def test_deleting_variantless_product_cascades_translations_and_tags(db_session: Session) -> None:
    make_product(db_session, "draft-only", tags=("hiking",), variants=[])
    db_session.commit()
    db_session.execute(delete(Product).where(Product.slug == "draft-only"))
    db_session.commit()
    assert db_session.query(ProductTranslation).count() == 0
    assert db_session.query(ProductTag).count() == 0


def test_country_belongs_to_one_zone(db_session: Session) -> None:
    europe = ShippingRate(zone_code="europe", name="Europe", price_minor=2000, currency="USD")
    other = ShippingRate(zone_code="other", name="Other", price_minor=100, currency="USD")
    db_session.add_all([europe, other])
    db_session.flush()
    db_session.add(ShippingRateCountry(country_code="FR", shipping_rate_id=europe.id))
    db_session.commit()
    _rejects(db_session, lambda s: s.add(ShippingRateCountry(country_code="FR", shipping_rate_id=other.id)))


@pytest.mark.parametrize("code", ["fr", "FRA"])
def test_country_code_format_enforced(db_session: Session, code: str) -> None:
    rate = ShippingRate(zone_code="z", name="Z", price_minor=1, currency="USD")
    db_session.add(rate)
    db_session.commit()
    _rejects(db_session, lambda s: s.add(ShippingRateCountry(country_code=code, shipping_rate_id=rate.id)))


def test_negative_shipping_price_rejected(db_session: Session) -> None:
    _rejects(db_session, lambda s: s.add(ShippingRate(zone_code="z", name="Z", price_minor=-1, currency="USD")))
