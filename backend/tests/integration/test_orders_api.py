"""Orders over HTTP (M3.1): create, recover, read, cancel, ownership, same-origin, no-store,
the cart's ``checkout`` field and lock, and the checkout config. Runs on SQLite and PostgreSQL.
Locking and races are covered at the service level (test_order_concurrency.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.clock import FixedClock
from app.db import create_db_engine, session_factory
from app.domain.orders.models import Order
from app.main import create_app
from tests.cart_api_support import ORIGIN, assert_no_leaks, build_shop, cookie_attributes, set_cookie_headers
from tests.conftest import make_settings
from tests.invariants import assert_inventory_invariants
from tests.order_support import EU_SHIPPING_MINOR, HOLD, add_shipping, count, order_row, stock
from tests.reservation_builders import NOW, TTL


@pytest.fixture
def shop_url(migrated_database_url: str) -> str:
    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        add_shipping(session)
        build_shop(session)  # commits
    engine.dispose()
    return migrated_database_url


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


def client_for(url: str, clock: FixedClock) -> TestClient:
    return TestClient(create_app(make_settings(url), clock=clock), headers={"Origin": ORIGIN})


@pytest.fixture
def api(shop_url: str, clock: FixedClock) -> Iterator[TestClient]:
    with client_for(shop_url, clock) as client:
        yield client


@pytest.fixture
def db(shop_url: str) -> Iterator[Session]:
    engine = create_db_engine(shop_url)
    with session_factory(engine)() as session:
        yield session
        session.rollback()
        assert_inventory_invariants(session)
    engine.dispose()


def error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()["error"]
    assert body["code"] == code
    return body


def reserved_bag(api: TestClient, lines: dict[str, int] | None = None) -> str:
    """Fill and reserve the client's bag; returns the cart token."""
    token = ""
    for sku, quantity in (lines or {"SHELL-M": 1, "SOCK-M": 2}).items():
        response = api.put(f"/api/cart/items/{sku}", json={"quantity": quantity})
        assert response.status_code == 200, response.text
        for header in set_cookie_headers(response):
            token = cookie_attributes(header)[1] or token
    assert api.post("/api/cart/reservation").status_code == 201
    return token


def create(api: TestClient, country: str = "FR", **kwargs: Any) -> httpx.Response:
    return api.post("/api/cart/orders", json={"ship_country": country}, **kwargs)


# --- create and recover ------------------------------------------------------------------------


def test_create_returns_the_server_snapshot(api: TestClient, db: Session) -> None:
    token = reserved_bag(api)
    response = create(api, params={"lang": "es"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "ref",
        "status",
        "version_hash",
        "lines",
        "item_count",
        "subtotal",
        "shipping",
        "total",
        "hold_expires_at",
        "created_at",
    }
    assert body["status"] == "pending_approval"
    assert len(body["ref"]) == 32
    assert len(body["version_hash"]) == 64
    assert [(line["sku"], line["quantity"], line["line_total"]["amount_minor"]) for line in body["lines"]] == [
        ("SHELL-M", 1, 12900),
        ("SOCK-M", 2, 3200),
    ]
    assert body["lines"][0]["name"] == "Chaqueta impermeable"  # live name in the requested language
    assert body["lines"][1]["fallback"] is True
    assert body["item_count"] == 3
    assert body["subtotal"] == {"amount_minor": 16100, "currency": "USD"}
    assert body["shipping"] == {
        "country": "FR",
        "zone": "europe",
        "price": {"amount_minor": EU_SHIPPING_MINOR, "currency": "USD"},
    }
    assert body["total"] == {"amount_minor": 16100 + EU_SHIPPING_MINOR, "currency": "USD"}
    assert body["hold_expires_at"].startswith((NOW + HOLD).strftime("%Y-%m-%dT%H:%M"))
    assert response.headers["cache-control"] == "no-store"
    assert_no_leaks(response, token)
    assert stock(db, "SHELL-M")[1] == 1  # still the one hold, now the order's


def test_language_changes_the_presentation_not_the_snapshot(api: TestClient) -> None:
    reserved_bag(api)
    created = create(api).json()
    read = api.get(f"/api/cart/orders/{created['ref']}", params={"lang": "es"}).json()
    assert read["version_hash"] == created["version_hash"]
    assert read["total"] == created["total"]
    assert read["lines"][0]["name"] != created["lines"][0]["name"]


def test_retrying_create_recovers_the_open_order(api: TestClient, db: Session) -> None:
    reserved_bag(api)
    first = create(api)
    retry = create(api)  # the client timed out after the first request committed
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json() == first.json()
    assert count(db, Order) == 1
    cart = api.get("/api/cart").json()
    assert cart["checkout"] == {"order_ref": first.json()["ref"], "status": "pending_approval"}
    assert cart["reservation"] is None  # the hold now belongs to the order


def test_create_errors(api: TestClient, db: Session) -> None:
    error(api.post("/api/cart/orders", json={"ship_country": "FR"}), 404, "cart_not_found")  # no cookie
    api.put("/api/cart/items/SHELL-M", json={"quantity": 1})
    error(create(api), 409, "reservation_required")
    api.post("/api/cart/reservation")
    error(create(api, "US"), 422, "unsupported_destination")
    error(create(api, "France"), 422, "invalid_country_code")
    error(api.post("/api/cart/orders", json={}), 422, "invalid_request")
    error(api.post("/api/cart/orders", json={"ship_country": "FR", "total": 1}), 422, "invalid_request")
    assert count(db, Order) == 0


# --- read, cancel, ownership -------------------------------------------------------------------


def test_read_and_cancel(api: TestClient, db: Session) -> None:
    reserved_bag(api)
    ref = create(api).json()["ref"]
    read = api.get(f"/api/cart/orders/{ref}")
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"

    cancelled = api.post(f"/api/cart/orders/{ref}/cancel")
    again = api.post(f"/api/cart/orders/{ref}/cancel")
    assert cancelled.status_code == again.status_code == 200
    assert cancelled.json()["status"] == again.json()["status"] == "cancelled"
    assert order_row(db, ref).reservation_status == "released"
    assert stock(db, "SHELL-M")[1] == 0
    assert api.get("/api/cart").json()["checkout"] is None
    assert api.put("/api/cart/items/SHELL-M", json={"quantity": 2}).status_code == 200


def test_another_carts_order_is_not_found(shop_url: str, clock: FixedClock, api: TestClient, db: Session) -> None:
    reserved_bag(api)
    ref = create(api).json()["ref"]
    with client_for(shop_url, clock) as stranger:
        reserved_bag(stranger, {"SOCK-M": 1})
        error(stranger.get(f"/api/cart/orders/{ref}"), 404, "order_not_found")
        error(stranger.post(f"/api/cart/orders/{ref}/cancel"), 404, "order_not_found")
    with client_for(shop_url, clock) as anonymous:  # no cookie at all
        error(anonymous.get(f"/api/cart/orders/{ref}"), 404, "order_not_found")
        error(anonymous.post(f"/api/cart/orders/{ref}/cancel"), 404, "order_not_found")
    assert order_row(db, ref).status == "pending_approval"


@pytest.mark.parametrize("ref", ["F" * 32, "0" * 31, "0" * 33, "not-a-ref"])
def test_malformed_refs_are_rejected(api: TestClient, ref: str) -> None:
    error(api.get(f"/api/cart/orders/{ref}"), 422, "invalid_request")


def test_the_hold_expires_on_read(api: TestClient, clock: FixedClock, db: Session) -> None:
    reserved_bag(api)
    ref = create(api).json()["ref"]
    clock.advance(HOLD)
    assert api.get(f"/api/cart/orders/{ref}").json()["status"] == "expired"
    assert order_row(db, ref).reservation_status == "expired"
    assert stock(db, "SHELL-M")[1] == 0


# --- the bag while an order is open ------------------------------------------------------------


def test_cart_changes_are_refused_with_the_order_ref(api: TestClient, clock: FixedClock) -> None:
    reserved_bag(api)
    ref = create(api).json()["ref"]
    clock.advance(TTL)  # past the cart hold: the order's hold is what counts now
    for response in (
        api.put("/api/cart/items/SHELL-M", json={"quantity": 2}),
        api.delete("/api/cart/items/SOCK-M"),
        api.post("/api/cart/reservation"),
        api.delete("/api/cart/reservation"),
    ):
        body = error(response, 409, "checkout_in_progress")
        assert body["details"] == [{"order_ref": ref}]
    assert api.get("/api/cart").json()["checkout"]["order_ref"] == ref


# --- same origin, config -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}],
)
def test_order_changes_require_the_same_origin(api: TestClient, db: Session, headers: dict[str, str]) -> None:
    reserved_bag(api)
    error(create(api, headers=headers), 403, "cross_origin_request")
    ref = create(api).json()["ref"]
    error(api.post(f"/api/cart/orders/{ref}/cancel", headers=headers), 403, "cross_origin_request")
    assert order_row(db, ref).status == "pending_approval"


def test_requests_without_an_origin_are_refused(shop_url: str, clock: FixedClock) -> None:
    with TestClient(create_app(make_settings(shop_url), clock=clock)) as bare:
        error(bare.post("/api/cart/orders", json={"ship_country": "FR"}), 403, "cross_origin_request")


def test_checkout_config_reports_payments_disabled(api: TestClient) -> None:
    response = api.get("/api/checkout/config")
    assert response.status_code == 200
    assert response.json() == {
        "payments_enabled": False,
        "paypal_client_id": None,
        "currency": "USD",
        "intent": "capture",
    }
