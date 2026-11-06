"""Validation of the committed seed files (no database)."""

import re

import pytest
from pydantic import ValidationError

from app.domain.catalog.vocab import LANGUAGES, SKU_PATTERN
from app.seed import (
    CatalogSeed,
    SeedError,
    SeedProduct,
    SeedVariant,
    ShippingSeed,
    load_seeds,
    translation_problems,
)


@pytest.fixture(scope="module")
def seeds() -> tuple[CatalogSeed, ShippingSeed]:
    return load_seeds()


def test_catalogue_size(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    catalog, _ = seeds
    assert len(catalog.products) == 20
    assert sum(len(product.variants) for product in catalog.products) == 60
    assert {product.category for product in catalog.products} == {
        "jackets",
        "base_layers",
        "trousers",
        "socks",
        "accessories",
    }


def test_every_product_has_all_five_languages(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    catalog, _ = seeds
    for product in catalog.products:
        assert set(product.translations) == set(LANGUAGES), product.slug


def test_brand_currency_and_machine_translation_disclosure(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    catalog, _ = seeds
    assert catalog.brand == "Atlas Demo Outfitters"
    assert catalog.currency == "USD"
    assert set(catalog.machine_translated_languages) == {"es", "fr", "de", "pt-BR"}
    assert "not been reviewed by native speakers" in catalog.notice


def test_skus_and_prices(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    catalog, _ = seeds
    skus = [variant.sku for product in catalog.products for variant in product.variants]
    assert len(skus) == len(set(skus))
    for product in catalog.products:
        for variant in product.variants:
            assert re.match(SKU_PATTERN, variant.sku)
            assert type(variant.price_minor) is int  # USD cents, never floats
            assert variant.price_minor > 0


def test_demo_edge_cases_present(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    catalog, _ = seeds
    variants = [variant for product in catalog.products for variant in product.variants]
    assert any(variant.initial_stock == 0 for variant in variants), "an out-of-stock variant"
    assert any(0 < variant.initial_stock <= variant.low_stock_threshold for variant in variants), "a low-stock variant"


def test_translations_contain_no_protected_values(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    catalog, _ = seeds
    skus = {variant.sku for product in catalog.products for variant in product.variants}
    for product in catalog.products:
        assert translation_problems(product, skus) == []


def _product(**translations: tuple[str, str]) -> SeedProduct:
    texts = {"en": ("Rain Shell", "A 2.5-layer jacket.")}
    texts.update({key.replace("_", "-"): value for key, value in translations.items()})
    for lang in LANGUAGES:
        texts.setdefault(lang, texts["en"])
    return SeedProduct.model_validate(
        {
            "slug": "rain-shell",
            "category": "jackets",
            "tags": ["waterproof"],
            "sort_order": 1,
            "translations": {lang: {"name": name, "description": desc} for lang, (name, desc) in texts.items()},
            "variants": [
                {
                    "sku": "ADO-RS-BLK-M",
                    "size": "M",
                    "colour": "black",
                    "price_minor": 100,
                    "initial_stock": 1,
                    "low_stock_threshold": 0,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("translation", "problem"),
    [
        (("Chaqueta", "Una chaqueta de 2,5 capas por $19.99."), "currency"),
        (("Chaqueta", "Una chaqueta de 2,5 capas, 20 dólares."), "currency"),
        (("Chaqueta", "Una chaqueta de 2,5 capas. https://example.com"), "URL"),
        (("Chaqueta", "Una chaqueta de 2,5 capas ADO-RS-BLK-M."), "SKU"),
        (("Chaqueta", "Una chaqueta de 3 capas."), "numbers differ"),
    ],
)
def test_translation_problems_detected(translation: tuple[str, str], problem: str) -> None:
    product = _product(es=translation)
    problems = translation_problems(product, {"ADO-RS-BLK-M"})
    assert any(problem in item for item in problems), problems


def test_seed_product_requires_all_languages() -> None:
    with pytest.raises(ValidationError, match="missing translations"):
        SeedProduct.model_validate(
            {
                "slug": "x",
                "category": "jackets",
                "tags": ["waterproof"],
                "sort_order": 1,
                "translations": {"en": {"name": "X", "description": "Y"}},
                "variants": [
                    {
                        "sku": "X-1",
                        "size": "M",
                        "colour": "black",
                        "price_minor": 1,
                        "initial_stock": 0,
                        "low_stock_threshold": 0,
                    }
                ],
            }
        )


@pytest.mark.parametrize("price", [129.0, "12900", True, 0, -1])
def test_seed_rejects_non_integer_or_non_positive_prices(price: object) -> None:
    with pytest.raises(ValidationError):
        SeedVariant.model_validate(
            {
                "sku": "X-1",
                "size": "M",
                "colour": "black",
                "price_minor": price,
                "initial_stock": 0,
                "low_stock_threshold": 0,
            }
        )


def test_currency_mismatch_is_refused() -> None:
    with pytest.raises(SeedError, match="does not match STORE_CURRENCY"):
        load_seeds(store_currency="EUR")


def test_shipping_zones_match_approved_d7_configuration(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    _, shipping = seeds
    assert shipping.origin_country == "IN"
    assert shipping.currency == "USD"
    assert {zone.zone_code: zone.price_minor for zone in shipping.zones} == {
        "asia_pacific": 1500,
        "europe": 2000,
        "north_america": 2200,
        "south_america": 2800,
    }
    destinations = {country for zone in shipping.zones for country in zone.countries}
    assert "IN" not in destinations
    assert {"FR", "DE", "ES", "PT", "BR", "US", "MX", "JP"} <= destinations


def test_shipping_rates_labelled_fictional(seeds: tuple[CatalogSeed, ShippingSeed]) -> None:
    _, shipping = seeds
    assert "Fictional demo configuration" in shipping.notice
    assert "not real or carrier-validated" in shipping.notice


def test_shipping_seed_rejects_country_in_two_zones() -> None:
    with pytest.raises(ValidationError, match="more than one zone"):
        ShippingSeed.model_validate(
            {
                "version": 1,
                "origin_country": "IN",
                "notice": "x",
                "currency": "USD",
                "zones": [
                    {"zone_code": "a", "name": "A", "price_minor": 1, "countries": ["FR"]},
                    {"zone_code": "b", "name": "B", "price_minor": 2, "countries": ["FR"]},
                ],
            }
        )
