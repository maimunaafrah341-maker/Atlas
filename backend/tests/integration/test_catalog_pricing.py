"""price_range and price sorting follow the variants that match the active filters.

The seed has one price per product, so these tests build products whose variants differ in
price and stock. Regression: before M2-prep, price_range covered every active variant, so a
filtered listing could show "from $50" under min_price_minor=$200, and price_desc sorted by
the cheapest variant.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import create_app
from tests.conftest import make_settings
from tests.factories import V, make_product


@pytest.fixture
def priced_client(db_session: Session, migrated_database_url: str) -> Iterator[TestClient]:
    make_product(
        db_session,
        "range-jacket",
        sort_order=1,
        variants=[
            V(sku="RJ-S", size="S", price_minor=5_000),
            V(sku="RJ-M", size="M", price_minor=20_000),
            V(sku="RJ-L", size="L", price_minor=30_000, on_hand=0),  # out of stock
            V(sku="RJ-XL", size="XL", price_minor=99_000, status="archived"),  # never counted
        ],
    )
    make_product(db_session, "flat-jacket", sort_order=2, variants=[V(sku="FJ-M", size="M", price_minor=25_000)])
    make_product(
        db_session,
        "cheap-socks",
        category="socks",
        sort_order=3,
        variants=[V(sku="CS-M", size="M", price_minor=1_500), V(sku="CS-L", size="L", price_minor=1_800)],
    )
    db_session.commit()
    with TestClient(create_app(make_settings(migrated_database_url))) as client:
        yield client


def search(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/api/catalog/products", params=params)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def ranges(items: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    return {
        item["slug"]: (item["price_range"]["min"]["amount_minor"], item["price_range"]["max"]["amount_minor"])
        for item in items
    }


def test_unfiltered_range_covers_all_active_variants(priced_client: TestClient) -> None:
    assert ranges(search(priced_client))["range-jacket"] == (5_000, 30_000)


def test_min_price_filter_narrows_the_displayed_range(priced_client: TestClient) -> None:
    # The regression: this used to report (5_000, 30_000), i.e. "from $50" under a $200 minimum.
    assert ranges(search(priced_client, min_price_minor=20_000)) == {
        "range-jacket": (20_000, 30_000),
        "flat-jacket": (25_000, 25_000),
    }


def test_max_price_size_and_stock_filters_narrow_the_range(priced_client: TestClient) -> None:
    assert ranges(search(priced_client, max_price_minor=20_000))["range-jacket"] == (5_000, 20_000)
    assert ranges(search(priced_client, size="L"))["range-jacket"] == (30_000, 30_000)
    assert ranges(search(priced_client, in_stock=True))["range-jacket"] == (5_000, 20_000)


def test_variants_list_is_not_filtered(priced_client: TestClient) -> None:
    [item] = [item for item in search(priced_client, size="L") if item["slug"] == "range-jacket"]
    assert [(v["sku"], v["matches_filters"]) for v in item["variants"]] == [
        ("RJ-S", False),
        ("RJ-M", False),
        ("RJ-L", True),
    ]


def test_price_desc_sorts_by_highest_matching_price(priced_client: TestClient) -> None:
    # range-jacket's dearest variant ($300) beats flat-jacket ($250). The old sort used the
    # cheapest variant and put flat-jacket first.
    assert [item["slug"] for item in search(priced_client, sort="price_desc")] == [
        "range-jacket",
        "flat-jacket",
        "cheap-socks",
    ]
    # In stock only, range-jacket's dearest buyable variant is $200, below flat-jacket.
    assert [item["slug"] for item in search(priced_client, sort="price_desc", in_stock=True)] == [
        "flat-jacket",
        "range-jacket",
        "cheap-socks",
    ]


def test_price_asc_sorts_by_lowest_matching_price(priced_client: TestClient) -> None:
    assert [item["slug"] for item in search(priced_client, sort="price_asc")] == [
        "cheap-socks",
        "range-jacket",
        "flat-jacket",
    ]
    assert [item["slug"] for item in search(priced_client, sort="price_asc", min_price_minor=20_000)] == [
        "range-jacket",
        "flat-jacket",
    ]


FILTER_SETS: list[dict[str, Any]] = [
    {},
    {"min_price_minor": 1_600},
    {"max_price_minor": 20_000},
    {"min_price_minor": 2_000, "max_price_minor": 26_000},
    {"size": "M"},
    {"size": ["M", "L"]},
    {"in_stock": True},
    {"in_stock": True, "min_price_minor": 1_000},
    {"colour": "black", "size": "L"},
]


@pytest.mark.parametrize("filters", FILTER_SETS)
def test_range_matches_flagged_variants_and_sorts_agree(priced_client: TestClient, filters: dict[str, Any]) -> None:
    for item in search(priced_client, **filters):
        matching = [v["price"]["amount_minor"] for v in item["variants"] if v["matches_filters"]]
        assert matching, item["slug"]
        assert ranges([item])[item["slug"]] == (min(matching), max(matching))

    ascending = [low for low, _high in ranges(search(priced_client, sort="price_asc", **filters)).values()]
    assert ascending == sorted(ascending)
    descending = [high for _low, high in ranges(search(priced_client, sort="price_desc", **filters)).values()]
    assert descending == sorted(descending, reverse=True)


def test_detail_range_is_unfiltered(priced_client: TestClient) -> None:
    body = priced_client.get("/api/catalog/products/range-jacket").json()
    assert (body["price_range"]["min"]["amount_minor"], body["price_range"]["max"]["amount_minor"]) == (5_000, 30_000)
    assert all(variant["matches_filters"] for variant in body["variants"])
