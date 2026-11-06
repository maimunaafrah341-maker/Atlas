"""Catalogue search and detail API against the real seed. Expected orders are explicit, so the
same assertions passing on SQLite and PostgreSQL proves the results are identical."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import create_app
from tests.conftest import make_settings, open_session
from tests.factories import V, make_product


def search(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/catalog/products", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def slugs(body: dict[str, Any]) -> list[str]:
    return [item["slug"] for item in body["items"]]


def test_default_listing_is_featured_order(seeded_client: TestClient) -> None:
    body = search(seeded_client)
    assert body["total"] == 20
    assert (body["page"], body["page_size"], body["total_pages"]) == (1, 20, 1)
    assert slugs(body)[:3] == ["ridgeline-rain-shell", "summit-3l-shell", "ember-insulated-jacket"]
    assert slugs(body)[-1] == "multi-neck-gaiter"


def test_money_is_integer_usd_minor_units(seeded_client: TestClient) -> None:
    for item in search(seeded_client)["items"]:
        for money in (item["price_range"]["min"], item["price_range"]["max"]):
            assert type(money["amount_minor"]) is int
            assert money["currency"] == "USD"
        for variant in item["variants"]:
            assert type(variant["price"]["amount_minor"]) is int


def test_stock_is_exposed_as_status_only(seeded_client: TestClient) -> None:
    response = seeded_client.get("/api/catalog/products/ridgeline-rain-shell")
    statuses = {variant["sku"]: variant["stock_status"] for variant in response.json()["variants"]}
    assert statuses["ADO-RRS-NAV-M"] == "low_stock"  # 3 available, threshold 3
    assert statuses["ADO-RRS-NAV-L"] == "in_stock"
    trousers = seeded_client.get("/api/catalog/products/trekker-hiking-trousers").json()
    assert {v["sku"]: v["stock_status"] for v in trousers["variants"]}["ADO-THT-KHA-XL"] == "out_of_stock"
    for forbidden in ("stock_on_hand", "stock_reserved", "low_stock_threshold", "available"):
        assert forbidden not in response.text


def test_detail_in_requested_language_with_variant_order(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/catalog/products/ridgeline-rain-shell", params={"lang": "fr"}).json()
    assert body["name"] == "Veste imperméable Ridgeline"
    assert (body["lang"], body["fallback"], body["machine_translated"]) == ("fr", False, True)
    assert [(v["size"], v["colour"]) for v in body["variants"]] == [
        ("S", "navy"),
        ("S", "red"),
        ("M", "navy"),
        ("M", "red"),
        ("L", "navy"),
        ("L", "red"),
    ]
    english = seeded_client.get("/api/catalog/products/ridgeline-rain-shell").json()
    assert (english["lang"], english["machine_translated"]) == ("en", False)


def test_english_text_search_ranks_name_matches_first(seeded_client: TestClient) -> None:
    body = search(seeded_client, q="jacket")
    # Name matches (score 2) before description-only matches (score 1); ties by featured order.
    assert slugs(body) == ["ember-insulated-jacket", "breeze-wind-jacket", "ridgeline-rain-shell"]


def test_spanish_search_in_spanish_text(seeded_client: TestClient) -> None:
    body = search(seeded_client, q="chaqueta impermeable", lang="es")
    assert slugs(body) == ["ridgeline-rain-shell", "summit-3l-shell"]
    assert body["items"][0]["name"] == "Chaqueta impermeable Ridgeline"


def test_search_is_accent_and_case_insensitive(seeded_client: TestClient) -> None:
    with_accent = search(seeded_client, q="IMPERMEÁVEL", lang="pt-BR")
    without = search(seeded_client, q="impermeavel", lang="pt-BR")
    assert slugs(with_accent) == slugs(without) != []


def test_english_text_is_searchable_in_every_language(seeded_client: TestClient) -> None:
    assert "ridgeline-rain-shell" in slugs(search(seeded_client, q="rain shell", lang="de"))


def test_all_query_words_must_match(seeded_client: TestClient) -> None:
    assert search(seeded_client, q="merino socks")["total"] == 1
    assert search(seeded_client, q="merino xylophone")["total"] == 0


def test_demo_query_filters(seeded_client: TestClient) -> None:
    body = search(seeded_client, lang="es", category="jackets", tag="waterproof", size="M", max_price_minor=15000)
    assert slugs(body) == ["ridgeline-rain-shell"]  # the $219 shell is excluded by price
    matching = [v["sku"] for v in body["items"][0]["variants"] if v["matches_filters"]]
    assert matching == ["ADO-RRS-NAV-M", "ADO-RRS-RED-M"]


def test_tags_must_all_match(seeded_client: TestClient) -> None:
    assert slugs(search(seeded_client, tag=["waterproof", "winter"])) == ["summit-3l-shell"]


def test_categories_and_colours_match_any(seeded_client: TestClient) -> None:
    assert search(seeded_client, category=["socks", "accessories"])["total"] == 8
    assert slugs(search(seeded_client, colour="red")) == ["ridgeline-rain-shell", "merino-beanie"]


def test_in_stock_filter(seeded_client: TestClient) -> None:
    assert slugs(search(seeded_client, category="trousers", size="XL")) == ["trekker-hiking-trousers"]
    assert search(seeded_client, category="trousers", size="XL", in_stock=True)["total"] == 0


def test_price_filters_and_sorting(seeded_client: TestClient) -> None:
    assert slugs(search(seeded_client, min_price_minor=20000)) == ["summit-3l-shell"]
    assert slugs(search(seeded_client, sort="price_asc"))[0] == "liner-socks-two-pack"
    assert slugs(search(seeded_client, sort="price_desc"))[0] == "summit-3l-shell"
    prices = [item["price_range"]["min"]["amount_minor"] for item in search(seeded_client, sort="price_asc")["items"]]
    assert prices == sorted(prices)


def test_pagination(seeded_client: TestClient) -> None:
    pages = [search(seeded_client, page_size=7, page=page) for page in (1, 2, 3, 4)]
    assert [page["total"] for page in pages] == [20, 20, 20, 20]
    assert pages[0]["total_pages"] == 3
    assert [len(page["items"]) for page in pages] == [7, 7, 6, 0]
    seen = [slug for page in pages for slug in slugs(page)]
    assert len(seen) == len(set(seen)) == 20
    assert seen == slugs(search(seeded_client))


def test_results_are_deterministic(seeded_client: TestClient) -> None:
    params = {"q": "hiking", "sort": "relevance"}
    assert search(seeded_client, **params) == search(seeded_client, **params)


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"lang": "xx"}, "lang"),
        ({"lang": "pt"}, "lang"),
        ({"page_size": 500}, "page_size"),
        ({"page": 0}, "page"),
        ({"category": "boots"}, "category.0"),
        ({"tag": "fashionable"}, "tag.0"),
        ({"size": "XXXL"}, "size.0"),
        ({"min_price_minor": -1}, "min_price_minor"),
        ({"min_price_minor": 2.5}, "min_price_minor"),
        ({"q": "x" * 201}, "q"),
        ({"sort": "name"}, "sort"),
        ({"min_price_minor": 5000, "max_price_minor": 1000}, "min_price_minor"),
        ({"sort": "relevance"}, "sort"),
        ({"q": "!"}, "q"),
    ],
)
def test_invalid_parameters_return_error_envelope(
    seeded_client: TestClient, params: dict[str, Any], field: str
) -> None:
    response = seeded_client.get("/api/catalog/products", params=params)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert field in [detail["field"] for detail in error["details"]]
    assert "x" * 201 not in response.text  # submitted values are not echoed


def test_unknown_product_is_typed_404(seeded_client: TestClient) -> None:
    response = seeded_client.get("/api/catalog/products/no-such-thing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "product_not_found"


def test_malformed_slug_is_422(seeded_client: TestClient) -> None:
    assert seeded_client.get("/api/catalog/products/Bad_Slug").status_code == 422


@pytest.fixture
def custom_client(migrated_database_url: str) -> TestClient:
    for session in open_session(migrated_database_url):
        _add_edge_case_products(session)
    return TestClient(create_app(make_settings(migrated_database_url)))


def _add_edge_case_products(session: Session) -> None:
    make_product(
        session,
        "english-only",
        tags=("hiking",),
        translations={"en": ("Fallback Fleece", "Only written in English.")},
        sort_order=1,
    )
    make_product(session, "draft-jacket", status="draft", sort_order=2)
    make_product(session, "archived-variants-only", variants=[V(sku="ARC-1", status="archived")], sort_order=3)
    make_product(
        session,
        "mixed-variants",
        variants=[V(sku="MIX-M", size="M"), V(sku="MIX-L", size="L", status="archived")],
        sort_order=4,
    )
    session.commit()


def test_missing_translation_falls_back_to_english(custom_client: TestClient) -> None:
    with custom_client:
        body = custom_client.get("/api/catalog/products/english-only", params={"lang": "de"}).json()
        assert (body["name"], body["lang"], body["fallback"]) == ("Fallback Fleece", "en", True)
        found = search(custom_client, q="fleece", lang="de")
        assert slugs(found) == ["english-only"]
        assert found["items"][0]["fallback"] is True


def test_inactive_products_and_variants_are_hidden(custom_client: TestClient) -> None:
    with custom_client:
        assert slugs(search(custom_client)) == ["english-only", "mixed-variants"]
        assert custom_client.get("/api/catalog/products/draft-jacket").status_code == 404
        assert custom_client.get("/api/catalog/products/archived-variants-only").status_code == 404
        mixed = custom_client.get("/api/catalog/products/mixed-variants").json()
        assert [variant["sku"] for variant in mixed["variants"]] == ["MIX-M"]
