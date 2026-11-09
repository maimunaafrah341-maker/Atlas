"""/api/cart over HTTP (M2.3): cookie lifecycle, cart and reservation flows, validation errors,
lazy expiry, headers, and that nothing internal leaks. Runs on SQLite locally and on
PostgreSQL in CI. Locking and races are covered at the service level
(test_reservation_concurrency.py); this layer adds none of its own.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clock import FixedClock
from app.db import create_db_engine, session_factory
from app.domain.cart.models import CART_IDLE_LIFETIME, Cart
from app.domain.catalog.models import Variant
from app.domain.reservations.models import Reservation
from app.logging_setup import JsonFormatter
from app.main import create_app
from app.middleware import SECURITY_HEADERS
from tests.cart_api_support import (
    ORIGIN,
    assert_no_leaks,
    build_shop,
    cart_count,
    cookie_attributes,
    movements,
    set_cookie_headers,
)
from tests.conftest import make_settings
from tests.invariants import assert_inventory_invariants
from tests.reservation_builders import NOW, TTL, stock

CART_LIFETIME_SECONDS = int(CART_IDLE_LIFETIME.total_seconds())


@pytest.fixture
def shop_url(migrated_database_url: str) -> str:
    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        build_shop(session)
    engine.dispose()
    return migrated_database_url


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def api(shop_url: str, clock: FixedClock) -> Iterator[TestClient]:
    app = create_app(make_settings(shop_url), clock=clock)
    with TestClient(app, headers={"Origin": ORIGIN}) as client:
        yield client


@pytest.fixture
def db(shop_url: str) -> Iterator[Session]:
    engine = create_db_engine(shop_url)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


def put(api: TestClient, sku: str, quantity: Any, **kwargs: Any) -> httpx.Response:
    return api.put(f"/api/cart/items/{sku}", json={"quantity": quantity}, **kwargs)


def token_of(response: httpx.Response) -> str:
    [header] = set_cookie_headers(response)
    name, value, _ = cookie_attributes(header)
    assert name == "atlas_cart"
    return value


def cart_row(db: Session) -> Cart:
    db.expire_all()
    return db.execute(select(Cart)).scalar_one()


def error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]
    assert isinstance(body["error"]["details"], list)
    detail: dict[str, Any] = body["error"]
    return detail


# --- Cookie lifecycle -------------------------------------------------------------------------


def test_get_without_cookie_is_an_empty_cart_and_creates_nothing(api: TestClient, db: Session) -> None:
    response = api.get("/api/cart")
    assert response.status_code == 200
    assert response.json() == {
        "lines": [],
        "item_count": 0,
        "subtotal": {"amount_minor": 0, "currency": "USD"},
        "expires_at": None,
        "reservation": None,
        "checkout": None,
    }
    assert set_cookie_headers(response) == []
    assert cart_count(db) == 0


def test_first_add_creates_the_cart_and_sets_a_hashed_token_cookie(api: TestClient, db: Session) -> None:
    response = put(api, "SHELL-M", 2)
    assert response.status_code == 200
    [header] = set_cookie_headers(response)
    name, token, attributes = cookie_attributes(header)
    assert name == "atlas_cart"
    assert len(token) == 43  # secrets.token_urlsafe(32)
    assert attributes == {
        "httponly": "",
        "max-age": str(CART_LIFETIME_SECONDS),
        "path": "/api/cart",
        "samesite": "lax",
    }  # no Secure outside production (see test_cart_api_security for production)
    cart = cart_row(db)
    assert cart.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert_no_leaks(response, token)


def test_first_add_that_fails_leaves_no_cart_and_no_cookie(api: TestClient, db: Session) -> None:
    for response, status, code in (
        (put(api, "NOPE-1", 1), 404, "variant_not_found"),
        (put(api, "SHELL-L", 5), 409, "insufficient_available_stock"),  # only 3 on hand
        (put(api, "SHELL-XL", 1), 409, "insufficient_available_stock"),
        (put(api, "SHELL-M", 11), 422, "invalid_quantity"),
    ):
        body = error(response, status, code)
        assert set_cookie_headers(response) == []
        assert_no_leaks(response)
        if code == "insufficient_available_stock":
            # The SKU and nothing about how many are left (D-5).
            assert all(set(detail) <= {"sku", "requested"} for detail in body["details"]), body
    assert cart_count(db) == 0


@pytest.mark.parametrize("cookie", ["not-a-real-token", "x" * 10_000, ""])
def test_invalid_or_unknown_cookie_is_removed_on_read(api: TestClient, db: Session, cookie: str) -> None:
    api.cookies.set("atlas_cart", cookie, path="/api/cart")
    response = api.get("/api/cart")
    assert response.status_code == 200
    assert response.json()["lines"] == []
    if cookie:
        [header] = set_cookie_headers(response)
        name, value, attributes = cookie_attributes(header)
        assert name == "atlas_cart"
        assert value in ("", '""')  # Starlette writes the empty value quoted
        # Deleted with the attributes it was set with, so the browser actually removes it.
        assert attributes["path"] == "/api/cart"
        assert attributes["max-age"] == "0"
        assert "httponly" in attributes
        assert attributes["samesite"] == "lax"
    assert cart_count(db) == 0


def test_unknown_cookie_on_add_starts_a_new_cart_with_a_new_token(api: TestClient, db: Session) -> None:
    api.cookies.set("atlas_cart", "forged-or-stale-token", path="/api/cart")
    response = put(api, "SOCK-M", 1)
    assert response.status_code == 200
    token = token_of(response)
    assert token != "forged-or-stale-token"
    assert cart_row(db).token_hash == hashlib.sha256(token.encode()).hexdigest()


def test_expired_cart_cookie_reads_as_empty_and_a_new_add_starts_a_new_cart(
    api: TestClient, db: Session, clock: FixedClock
) -> None:
    old = token_of(put(api, "SOCK-M", 1))
    clock.advance(CART_IDLE_LIFETIME + timedelta(seconds=1))

    read = api.get("/api/cart")
    assert read.json()["lines"] == []
    [deleted] = set_cookie_headers(read)
    assert cookie_attributes(deleted)[2]["max-age"] == "0"

    api.cookies.set("atlas_cart", old, path="/api/cart")
    new = token_of(put(api, "SOCK-M", 1))
    assert new != old
    assert cart_count(db) == 2  # the old cart is left as it was, never reused


def test_cookie_lifetime_follows_the_cart_and_reads_change_nothing(
    api: TestClient, db: Session, clock: FixedClock
) -> None:
    put(api, "SOCK-M", 1)
    before = cart_row(db)
    version, expires_at, updated_at = before.version, before.expires_at, before.updated_at
    clock.advance(timedelta(days=10))

    response = api.get("/api/cart")
    [header] = set_cookie_headers(response)
    assert cookie_attributes(header)[2]["max-age"] == str(CART_LIFETIME_SECONDS - 10 * 86400)
    after = cart_row(db)
    assert (after.version, after.expires_at, after.updated_at) == (version, expires_at, updated_at)


# --- Cart contents ----------------------------------------------------------------------------


def test_cart_response_lines_names_and_totals(api: TestClient) -> None:
    put(api, "SHELL-M", 2)
    response = put(api, "SOCK-M", 3)
    body = response.json()
    assert set(body) == {"lines", "item_count", "subtotal", "expires_at", "reservation", "checkout"}
    assert body["checkout"] is None
    assert body["lines"] == [
        {
            "sku": "SHELL-M",
            "product_slug": "rain-shell",
            "name": "Rain Shell",
            "lang": "en",
            "fallback": False,
            "machine_translated": False,
            "size": "M",
            "colour": "navy",
            "quantity": 2,
            "unit_price": {"amount_minor": 12900, "currency": "USD"},
            "line_total": {"amount_minor": 25800, "currency": "USD"},
            "available": True,
        },
        {
            "sku": "SOCK-M",
            "product_slug": "trail-sock",
            "name": "Trail Sock",
            "lang": "en",
            "fallback": False,
            "machine_translated": False,
            "size": "M",
            "colour": "grey",
            "quantity": 3,
            "unit_price": {"amount_minor": 1600, "currency": "USD"},
            "line_total": {"amount_minor": 4800, "currency": "USD"},
            "available": True,
        },
    ]
    assert body["item_count"] == 5
    assert body["subtotal"] == {"amount_minor": 30600, "currency": "USD"}
    assert body["expires_at"].startswith("2026-12-08T09:00:00")  # 30 days after NOW
    assert body["reservation"] is None


def test_names_follow_the_requested_language_with_catalogue_fallback(api: TestClient) -> None:
    put(api, "SHELL-M", 1)
    put(api, "SOCK-M", 1)
    lines = {line["sku"]: line for line in api.get("/api/cart", params={"lang": "es"}).json()["lines"]}
    shell, sock = lines["SHELL-M"], lines["SOCK-M"]
    assert (shell["name"], shell["lang"], shell["fallback"]) == ("Chaqueta impermeable", "es", False)
    assert shell["machine_translated"] is True  # the test factory flags non-English text
    assert (sock["name"], sock["lang"], sock["fallback"]) == ("Trail Sock", "en", True)
    error(api.get("/api/cart", params={"lang": "xx"}), 422, "invalid_request")


def test_repeating_the_same_quantity_changes_nothing(api: TestClient, db: Session, clock: FixedClock) -> None:
    first = put(api, "SOCK-M", 2)
    version = cart_row(db).version
    clock.advance(timedelta(hours=1))
    again = put(api, "SOCK-M", 2)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert cart_row(db).version == version


def test_removing_a_line_is_idempotent(api: TestClient) -> None:
    put(api, "SOCK-M", 1)
    put(api, "SHELL-M", 1)
    for _ in range(2):
        response = api.delete("/api/cart/items/SOCK-M")
        assert response.status_code == 200
        assert [line["sku"] for line in response.json()["lines"]] == ["SHELL-M"]
    assert api.delete("/api/cart/items/UNKNOWN-9").status_code == 200


def test_cart_endpoints_without_a_cart_succeed_without_creating_one(api: TestClient, db: Session) -> None:
    for response in (api.delete("/api/cart/items/SOCK-M"), api.delete("/api/cart/reservation")):
        assert response.status_code == 200
        assert response.json()["lines"] == []
        assert set_cookie_headers(response) == []
    error(api.post("/api/cart/reservation"), 404, "cart_not_found")
    assert cart_count(db) == 0


def test_archived_line_stays_visible_but_is_excluded_from_the_subtotal(api: TestClient, db: Session) -> None:
    put(api, "SOCK-M", 2)
    put(api, "SHELL-M", 1)
    db.execute(update(Variant).where(Variant.sku == "SOCK-M").values(status="archived"))
    db.commit()
    body = api.get("/api/cart").json()
    assert {line["sku"]: line["available"] for line in body["lines"]} == {"SOCK-M": False, "SHELL-M": True}
    assert body["item_count"] == 3
    assert body["subtotal"] == {"amount_minor": 12900, "currency": "USD"}
    error(put(api, "SOCK-M", 3), 409, "variant_inactive")


def test_line_limit_error_reaches_the_client(api: TestClient, db: Session) -> None:
    from tests.factories import V, make_product

    combos = [
        (size, colour)
        for size in ("S", "M", "L")
        for colour in ("black", "grey", "navy", "red", "blue", "green", "olive")
    ]
    make_product(
        db, "many", variants=[V(sku=f"MANY-{n}", size=sz, colour=c, on_hand=5) for n, (sz, c) in enumerate(combos)]
    )
    db.commit()
    for n in range(20):
        assert put(api, f"MANY-{n}", 1).status_code == 200
    error(put(api, "MANY-20", 1), 409, "cart_line_limit")


# --- Validation errors use the standard envelope ----------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"quantity": "2"},
        {"quantity": True},
        {"quantity": 2.5},
        {"quantity": None},
        {},
        {"quantity": 1, "price": 1},
        [1],
    ],
)
def test_invalid_bodies_are_invalid_request_without_echoing_input(api: TestClient, body: Any) -> None:
    response = api.put("/api/cart/items/SOCK-M", json=body)
    detail = error(response, 422, "invalid_request")
    assert all(set(item) == {"field", "issue"} for item in detail["details"])
    assert "price" not in response.text or detail["details"][0]["field"] == "price"


def test_malformed_and_non_json_bodies_are_invalid_request(api: TestClient) -> None:
    error(
        api.put("/api/cart/items/SOCK-M", content=b"{not json", headers={"Content-Type": "application/json"}),
        422,
        "invalid_request",
    )
    error(api.put("/api/cart/items/SOCK-M", data={"quantity": "1"}), 422, "invalid_request")


@pytest.mark.parametrize("sku", ["shell-m", "SHELL_M", "-SHELL", "A" * 65])
def test_malformed_skus_are_invalid_request(api: TestClient, sku: str) -> None:
    error(put(api, sku, 1), 422, "invalid_request")


@pytest.mark.parametrize("quantity", [0, -1, 11])
def test_out_of_range_quantities_are_the_services_invalid_quantity(api: TestClient, quantity: int) -> None:
    error(put(api, "SOCK-M", quantity), 422, "invalid_quantity")


# --- Reservations ------------------------------------------------------------------------------


def test_reserving_creates_once_and_repeating_never_renews(api: TestClient, db: Session, clock: FixedClock) -> None:
    put(api, "SHELL-L", 2)
    created = api.post("/api/cart/reservation")
    assert created.status_code == 201
    reservation = created.json()["reservation"]
    assert set(reservation) == {"id", "status", "expires_at", "items"}
    assert reservation["status"] == "active"
    assert reservation["items"] == [{"sku": "SHELL-L", "quantity": 2}]
    assert reservation["expires_at"].startswith((NOW + TTL).strftime("%Y-%m-%dT%H:%M:%S"))
    assert stock(db, "SHELL-L") == (3, 2)

    clock.advance(timedelta(minutes=5))
    repeated = api.post("/api/cart/reservation")
    assert repeated.status_code == 200
    assert repeated.json()["reservation"] == reservation  # same id, same expiry: not a renewal
    assert movements(db, "reservation_hold") == 1
    assert stock(db, "SHELL-L") == (3, 2)
    assert_inventory_invariants(db)


def test_any_edit_ends_the_reservation_but_a_repeated_quantity_does_not(api: TestClient, db: Session) -> None:
    put(api, "SHELL-L", 2)
    reservation = api.post("/api/cart/reservation").json()["reservation"]
    assert put(api, "SHELL-L", 2).json()["reservation"] == reservation  # nothing changed

    assert put(api, "SHELL-L", 1).json()["reservation"] is None
    db.expire_all()
    assert db.execute(select(Reservation.status)).scalar_one() == "released"
    assert stock(db, "SHELL-L") == (3, 0)
    assert_inventory_invariants(db)


def test_releasing_is_idempotent(api: TestClient, db: Session) -> None:
    put(api, "SHELL-L", 1)
    api.post("/api/cart/reservation")
    for _ in range(2):
        response = api.delete("/api/cart/reservation")
        assert response.status_code == 200
        assert response.json()["reservation"] is None
        assert [line["sku"] for line in response.json()["lines"]] == ["SHELL-L"]
    assert movements(db, "reservation_release") == 1
    assert stock(db, "SHELL-L") == (3, 0)
    assert_inventory_invariants(db)


def test_get_expires_an_overdue_reservation_exactly_once_and_changes_nothing_else(
    api: TestClient, db: Session, clock: FixedClock
) -> None:
    put(api, "SHELL-L", 2)
    put(api, "SOCK-M", 1)
    api.post("/api/cart/reservation")
    cart = cart_row(db)
    version, expires_at, updated_at = cart.version, cart.expires_at, cart.updated_at

    clock.advance(TTL + timedelta(seconds=1))
    for _ in range(3):
        body = api.get("/api/cart").json()
        assert body["reservation"] is None
        assert [line["quantity"] for line in body["lines"]] == [2, 1]  # lines untouched

    assert movements(db, "reservation_expiry") == 2  # one per item, released once
    assert movements(db, "reservation_release") == 0
    assert stock(db, "SHELL-L") == (3, 0)
    assert stock(db, "SOCK-M") == (50, 0)
    db.expire_all()
    assert db.execute(select(Reservation.status)).scalar_one() == "expired"
    cart = cart_row(db)
    assert (cart.version, cart.expires_at, cart.updated_at) == (version, expires_at, updated_at)
    assert_inventory_invariants(db)


def test_reservation_errors_reach_the_client_without_counts(api: TestClient, db: Session) -> None:
    put(api, "SOCK-M", 1)
    api.delete("/api/cart/items/SOCK-M")
    error(api.post("/api/cart/reservation"), 409, "cart_empty")

    put(api, "SHELL-L", 3)
    db.execute(update(Variant).where(Variant.sku == "SHELL-L").values(stock_on_hand=1))
    db.commit()
    shortfall = error(api.post("/api/cart/reservation"), 409, "insufficient_available_stock")
    assert shortfall["details"] == [{"sku": "SHELL-L"}]

    db.execute(update(Variant).where(Variant.sku == "SHELL-L").values(status="archived"))
    db.commit()
    unavailable = error(api.post("/api/cart/reservation"), 409, "cart_has_unavailable_items")
    assert unavailable["details"] == [{"sku": "SHELL-L"}]
    assert_inventory_invariants(db)


# --- Headers, leaks and logs ------------------------------------------------------------------


def test_every_cart_response_is_no_store_with_security_headers(api: TestClient) -> None:
    responses = [
        api.get("/api/cart"),
        put(api, "SOCK-M", 1),
        put(api, "NOPE-1", 1),  # 404
        put(api, "SOCK-M", "x"),  # 422 validation
        api.put("/api/cart/items/SOCK-M", json={"quantity": 1}, headers={"Origin": "https://evil.example"}),  # 403
        api.post("/api/cart/reservation"),
        api.get("/api/cart/no-such-route"),  # 404 from routing
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.request.url
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
    assert "cache-control" not in api.get("/api/catalog/products").headers  # only the cart


def test_no_internal_ids_versions_counts_or_tokens_in_any_response(api: TestClient, db: Session) -> None:
    first = put(api, "SHELL-M", 2)
    token = token_of(first)
    responses = [
        first,
        put(api, "SOCK-M", 1),
        api.get("/api/cart", params={"lang": "es"}),
        api.post("/api/cart/reservation"),
        api.post("/api/cart/reservation"),
        put(api, "SHELL-M", 11),  # invalid_quantity
        put(api, "SHELL-XL", 1),  # insufficient_available_stock
        api.delete("/api/cart/reservation"),
        api.delete("/api/cart/items/SOCK-M"),
    ]
    reservation_ids = {
        r.json()["reservation"]["id"] for r in responses if r.status_code < 300 and r.json()["reservation"]
    }
    db.expire_all()
    internal_id = db.execute(select(Reservation.id)).scalar_one()
    assert reservation_ids == {db.execute(select(Reservation.public_id)).scalar_one()}
    assert str(internal_id) not in reservation_ids
    for response in responses:
        assert_no_leaks(response, token)


def test_logs_never_contain_the_token_or_its_digest(
    shop_url: str, clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(make_settings(shop_url, log_level="DEBUG"), clock=clock)
    [handler] = [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]
    assert isinstance(handler, logging.StreamHandler)
    stream = io.StringIO()
    handler.setStream(stream)
    with TestClient(app, headers={"Origin": ORIGIN}) as client:
        token = token_of(put(client, "SHELL-L", 1))
        client.get("/api/cart")
        client.post("/api/cart/reservation")
        client.put("/api/cart/items/SHELL-L", json={"quantity": 1}, headers={"Origin": "https://evil.example"})
        client.delete("/api/cart/reservation")

        # A server error while the cookie is present: logged, without the token.
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("cart read failed")

        monkeypatch.setattr("app.api.cart.read_cart", boom)
        failed = client.get("/api/cart")
        assert failed.status_code == 500
        assert failed.headers["cache-control"] == "no-store"
        assert_no_leaks(failed, token)
    output = stream.getvalue()
    assert "cart read failed" in output  # the error was logged...
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert token not in output  # ...without the token
    assert digest not in output
    assert "atlas_cart" not in output
