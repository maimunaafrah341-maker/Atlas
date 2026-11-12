"""Payments over HTTP (M3.2) with the fake PayPal: the full flow, 503 without configuration,
ownership, same-origin, no-store, read-only GET, and log / response privacy. SQLite and PostgreSQL.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.clock import FixedClock
from app.db import create_db_engine, session_factory
from app.logging_setup import JsonFormatter
from app.main import create_app
from tests import fake_paypal as fake
from tests.cart_api_support import ORIGIN, assert_no_leaks, build_shop, cookie_attributes, set_cookie_headers
from tests.conftest import make_settings
from tests.invariants import assert_inventory_invariants
from tests.order_support import add_shipping, order_row, stock
from tests.reservation_builders import NOW

SANDBOX = {
    "paypal_mode": "sandbox",
    "paypal_client_id": "sandbox-client-id",
    "paypal_client_secret": "sandbox-client-secret-value",
    "paypal_merchant_id": fake.MERCHANT_ID,
}


@pytest.fixture
def shop_url(migrated_database_url: str) -> str:
    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        add_shipping(session)
        build_shop(session)
    engine.dispose()
    return migrated_database_url


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def paypal() -> fake.FakePayPal:
    return fake.FakePayPal()


def client_for(url: str, clock: FixedClock, paypal: fake.FakePayPal | None, **settings: Any) -> TestClient:
    app = create_app(make_settings(url, **settings), clock=clock, paypal=paypal)
    return TestClient(app, headers={"Origin": ORIGIN})


@pytest.fixture
def api(shop_url: str, clock: FixedClock, paypal: fake.FakePayPal) -> Iterator[TestClient]:
    with client_for(shop_url, clock, paypal, **SANDBOX) as client:
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


def reviewed_order(api: TestClient) -> tuple[dict[str, Any], str]:
    token = ""
    response = api.put("/api/cart/items/SHELL-M", json={"quantity": 1})
    for header in set_cookie_headers(response):
        token = cookie_attributes(header)[1] or token
    assert api.post("/api/cart/reservation").status_code == 201
    order = api.post("/api/cart/orders", json={"ship_country": "FR"}).json()
    return order, token


def start(api: TestClient, order: dict[str, Any], **kwargs: Any) -> httpx.Response:
    return api.post(
        f"/api/cart/orders/{order['ref']}/paypal-order", json={"version_hash": order["version_hash"]}, **kwargs
    )


def capture(api: TestClient, order: dict[str, Any], paypal_order_id: str, **kwargs: Any) -> httpx.Response:
    return api.post(f"/api/cart/orders/{order['ref']}/capture", json={"paypal_order_id": paypal_order_id}, **kwargs)


# --- the flow ----------------------------------------------------------------------------------


def test_sandbox_checkout_end_to_end(api: TestClient, paypal: fake.FakePayPal, db: Session) -> None:
    order, token = reviewed_order(api)

    started = start(api, order)
    assert started.status_code == 200, started.text
    paypal_order_id = started.json()["paypal_order_id"]
    assert started.json()["order"]["status"] == "awaiting_payment"
    context = paypal.orders[paypal_order_id].body["payment_source"]["paypal"]["experience_context"]
    # Built from the site's own origin (scheme + Host), where PayPal returns the buyer.
    assert (context["return_url"], context["cancel_url"]) == (
        f"{ORIGIN}/bag?paypal=approved",
        f"{ORIGIN}/bag?paypal=cancelled",
    )
    paypal.approve(paypal_order_id)  # the buyer, in PayPal's window

    paid = capture(api, order, paypal_order_id)
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert (body["result"], body["recoverable"], body["order"]["status"]) == ("paid", False, "paid")
    assert paid.headers["cache-control"] == "no-store"
    assert_no_leaks(paid, token)
    assert stock(db, "SHELL-M")[1] == 0
    cart = api.get("/api/cart").json()
    assert (cart["lines"], cart["checkout"]) == ([], None)  # the bag was bought and is free again


def test_get_order_is_read_only_for_payments(api: TestClient, paypal: fake.FakePayPal, db: Session) -> None:
    order, _ = reviewed_order(api)
    paypal_order_id = start(api, order).json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    paypal.capture_outcomes.append(fake.TIMEOUT_AFTER_CAPTURE)
    assert capture(api, order, paypal_order_id).json()["result"] == "processing"
    calls = len(paypal.calls)

    assert api.get(f"/api/cart/orders/{order['ref']}").json()["status"] == "capturing"
    assert len(paypal.calls) == calls  # GET never calls PayPal or changes the payment

    reconciled = api.post(f"/api/cart/orders/{order['ref']}/reconcile")
    assert reconciled.json()["result"] == "paid"
    assert order_row(db, order["ref"]).status == "paid"


def test_instrument_declined_is_a_distinct_recoverable_result(api: TestClient, paypal: fake.FakePayPal) -> None:
    order, _ = reviewed_order(api)
    paypal_order_id = start(api, order).json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    paypal.capture_outcomes.append(fake.INSTRUMENT_DECLINED)
    body = capture(api, order, paypal_order_id).json()
    assert (body["result"], body["recoverable"], body["order"]["status"]) == (
        "instrument_declined",
        True,
        "awaiting_payment",
    )


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.parametrize("settings", [{}, {**SANDBOX, "paypal_mode": "live"}, {**SANDBOX, "paypal_client_secret": ""}])
def test_without_a_complete_sandbox_configuration_payments_are_503_and_the_shop_works(
    shop_url: str, clock: FixedClock, settings: dict[str, Any]
) -> None:
    with client_for(shop_url, clock, None, **settings) as api:
        assert api.get("/api/catalog/products").status_code == 200
        order, _ = reviewed_order(api)  # bag, reservation and order still work
        assert order["status"] == "pending_approval"
        error(start(api, order), 503, "payments_unavailable")
        error(capture(api, order, "PAYPAL-ORDER-0001"), 503, "payments_unavailable")
        error(api.post(f"/api/cart/orders/{order['ref']}/reconcile"), 503, "payments_unavailable")
        assert api.get("/api/checkout/config").json() == {
            "payments_enabled": False,
            "paypal_client_id": None,
            "currency": "USD",
            "intent": "capture",
        }
        assert api.post(f"/api/cart/orders/{order['ref']}/cancel").status_code == 200


def test_config_reports_the_public_client_id_only(api: TestClient) -> None:
    response = api.get("/api/checkout/config")
    assert response.json() == {
        "payments_enabled": True,
        "paypal_client_id": "sandbox-client-id",
        "currency": "USD",
        "intent": "capture",
    }
    assert "sandbox-client-secret-value" not in response.text


# --- protections -------------------------------------------------------------------------------


def test_payment_endpoints_need_the_owner(
    shop_url: str, clock: FixedClock, api: TestClient, paypal: fake.FakePayPal
) -> None:
    order, _ = reviewed_order(api)
    paypal_order_id = start(api, order).json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    with client_for(shop_url, clock, paypal, **SANDBOX) as stranger:
        reviewed_order(stranger)
        error(start(stranger, order), 404, "order_not_found")
        error(capture(stranger, order, paypal_order_id), 404, "order_not_found")
        error(stranger.post(f"/api/cart/orders/{order['ref']}/reconcile"), 404, "order_not_found")
    assert paypal.calls_to("capture_order") == []


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://evil.example"}, {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}]
)
def test_payment_endpoints_require_the_same_origin(
    api: TestClient, paypal: fake.FakePayPal, headers: dict[str, str]
) -> None:
    order, _ = reviewed_order(api)
    error(start(api, order, headers=headers), 403, "cross_origin_request")
    paypal_order_id = start(api, order).json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    error(capture(api, order, paypal_order_id, headers=headers), 403, "cross_origin_request")
    error(api.post(f"/api/cart/orders/{order['ref']}/reconcile", headers=headers), 403, "cross_origin_request")
    assert paypal.calls_to("capture_order") == []


def test_browser_input_is_validated_and_decides_nothing(api: TestClient, paypal: fake.FakePayPal) -> None:
    order, _ = reviewed_order(api)
    error(api.post(f"/api/cart/orders/{order['ref']}/paypal-order", json={"version_hash": "x"}), 422, "invalid_request")
    error(start(api, {**order, "version_hash": "0" * 64}), 409, "order_changed")
    paypal_order_id = start(api, order).json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    forged = {"paypal_order_id": paypal_order_id, "status": "COMPLETED"}
    error(api.post(f"/api/cart/orders/{order['ref']}/capture", json=forged), 422, "invalid_request")
    error(capture(api, order, "../../v1/oauth2/token"), 422, "invalid_request")
    error(capture(api, order, "PAYPAL-ORDER-9999"), 409, "paypal_order_mismatch")
    assert paypal.calls_to("capture_order") == []


def test_logs_carry_no_secrets_or_payer_data(api: TestClient, paypal: fake.FakePayPal) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    level = root.level
    root.setLevel(logging.INFO)
    try:
        order, token = reviewed_order(api)
        paypal_order_id = start(api, order).json()["paypal_order_id"]
        paypal.approve(paypal_order_id)
        paypal.capture_outcomes.append(fake.SERVER_ERROR_BEFORE)
        capture(api, order, paypal_order_id)
        api.post(f"/api/cart/orders/{order['ref']}/reconcile")
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    logs = stream.getvalue()
    assert "paypal capture outcome unknown" in logs
    for secret in ("sandbox-client-secret-value", token, "access-token"):
        assert secret not in logs
