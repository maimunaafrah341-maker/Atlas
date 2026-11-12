"""POST /api/webhooks/paypal over HTTP (M3.3 Stage 4), with the fake PayPal. SQLite and PostgreSQL.

PayPal's servers have no cart cookie, no Origin and no browser headers: these tests deliver
from a separate client with none of them. They prove the response mapping (2xx settles, 4xx
refuses, 503 retries), the HTTP-level body limit, header handling, no-store and no cookie,
that the route never captures, and that payment and inventory invariants hold.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import FixedClock
from app.db import create_db_engine, session_factory
from app.domain.payments.models import PayPalWebhookEvent
from app.domain.payments.paypal import PayPalUnavailable
from app.domain.payments.webhooks import COMPLETED, DENIED, MAX_EVENT_BYTES, PENDING
from app.logging_setup import JsonFormatter
from app.main import create_app
from tests import fake_paypal as fake
from tests.cart_api_support import ORIGIN, build_shop
from tests.conftest import make_settings
from tests.integration.test_paypal_webhooks import PAYER_EMAIL, TRANSMISSION
from tests.invariants import assert_inventory_invariants
from tests.order_support import add_shipping, order_row, stock
from tests.reservation_builders import NOW

SANDBOX = {
    "paypal_mode": "sandbox",
    "paypal_client_id": "sandbox-client-id",
    "paypal_client_secret": "sandbox-client-secret-value",
    "paypal_merchant_id": fake.MERCHANT_ID,
    "paypal_webhook_id": fake.WEBHOOK_ID,
}
URL = "/api/webhooks/paypal"
HEADERS = {
    "PAYPAL-AUTH-ALGO": TRANSMISSION.auth_algo,
    "PAYPAL-CERT-URL": TRANSMISSION.cert_url,
    "PAYPAL-TRANSMISSION-ID": TRANSMISSION.transmission_id,
    "PAYPAL-TRANSMISSION-SIG": TRANSMISSION.transmission_sig,
    "PAYPAL-TRANSMISSION-TIME": TRANSMISSION.transmission_time,
    "Content-Type": "application/json",
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


@pytest.fixture
def shopper(shop_url: str, clock: FixedClock, paypal: fake.FakePayPal) -> Iterator[TestClient]:
    """The buyer's browser: cart cookie and Origin."""
    app = create_app(make_settings(shop_url, **SANDBOX), clock=clock, paypal=paypal)
    with TestClient(app, headers={"Origin": ORIGIN}) as client:
        yield client


@pytest.fixture
def webhook(shopper: TestClient) -> TestClient:
    """PayPal's servers: the same app, but no cookie jar contents, no Origin, no browser headers."""
    return TestClient(shopper.app)


@pytest.fixture
def db(shop_url: str) -> Iterator[Session]:
    engine = create_db_engine(shop_url)
    with session_factory(engine)() as session:
        yield session
        session.rollback()
        assert_inventory_invariants(session)
    engine.dispose()


def lost_capture(shopper: TestClient, paypal: fake.FakePayPal) -> tuple[str, str]:
    """A buyer approved and Atlas captured, but the capture response was lost: order capturing."""
    assert shopper.put("/api/cart/items/SHELL-M", json={"quantity": 1}).status_code == 200
    assert shopper.post("/api/cart/reservation").status_code == 201
    order = shopper.post("/api/cart/orders", json={"ship_country": "FR"}).json()
    started = shopper.post(
        f"/api/cart/orders/{order['ref']}/paypal-order", json={"version_hash": order["version_hash"]}
    )
    paypal_order_id = started.json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    paypal.capture_outcomes.append(fake.TIMEOUT_AFTER_CAPTURE)
    captured = shopper.post(f"/api/cart/orders/{order['ref']}/capture", json={"paypal_order_id": paypal_order_id})
    assert captured.json()["result"] == "processing"
    return order["ref"], paypal_order_id


def event(event_type: str, paypal_order_id: str, ref: str | None = None, event_id: str = "WH-API-1") -> bytes:
    resource: dict[str, Any] = {
        "id": "CAPTURE-CLAIMED",
        "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": "1.00"},  # a lie: never used
        "payer": {"email_address": PAYER_EMAIL},
        "supplementary_data": {"related_ids": {"order_id": paypal_order_id}},
    }
    if ref:
        resource["invoice_id"] = ref
    return json.dumps({"id": event_id, "event_type": event_type, "resource": resource}).encode()


def deliver(client: TestClient, raw: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    return client.post(URL, content=raw, headers=HEADERS if headers is None else headers)


def error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def events(db: Session) -> list[PayPalWebhookEvent]:
    db.rollback()
    return list(db.execute(select(PayPalWebhookEvent)).scalars())


# --- the flow ------------------------------------------------------------------------------------


def test_a_completed_event_pays_the_order_without_cookie_origin_or_csrf_headers(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    assert not webhook.cookies  # PayPal has no cart cookie
    response = deliver(webhook, event(COMPLETED, paypal_order_id, ref))
    assert response.status_code == 200, response.text
    assert response.json() == {"received": True, "event_type": COMPLETED, "outcome": "paid", "duplicate": False}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "origin" not in {name.lower() for name in response.request.headers}
    assert order_row(db, ref).status == "paid"
    assert stock(db, "SHELL-M")[1] == 0  # the hold was committed as a sale
    assert len(paypal.calls_to("capture_order")) == 1  # only the buyer's capture; the webhook never captures


def test_a_duplicate_delivery_is_acknowledged_once_more_and_changes_nothing(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    raw = event(COMPLETED, paypal_order_id, ref)
    assert deliver(webhook, raw).json()["duplicate"] is False
    reads = len(paypal.calls_to("get_order"))
    again = deliver(webhook, raw)
    assert again.status_code == 200
    assert again.json() == {"received": True, "event_type": COMPLETED, "outcome": "paid", "duplicate": True}
    assert len(paypal.calls_to("get_order")) == reads  # settled: PayPal isn't asked again
    (row,) = events(db)
    assert (row.deliveries, row.outcome) == (2, "paid")
    assert len(paypal.calls_to("capture_order")) == 1


def test_a_foreign_origin_and_cross_site_fetch_metadata_do_not_matter(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    headers = {**HEADERS, "Origin": "https://paypal.example", "Sec-Fetch-Site": "cross-site"}
    assert deliver(webhook, event(COMPLETED, paypal_order_id, ref), headers).json()["outcome"] == "paid"


def test_the_body_never_decides_the_outcome(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    # A DENIED event while PayPal shows the capture COMPLETED: PayPal's state wins.
    assert deliver(webhook, event(DENIED, paypal_order_id, ref)).json()["outcome"] == "paid"
    assert order_row(db, ref).status == "paid"


def test_a_pending_event_keeps_the_hold_and_settles(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    assert shopper.put("/api/cart/items/SHELL-M", json={"quantity": 1}).status_code == 200
    assert shopper.post("/api/cart/reservation").status_code == 201
    order = shopper.post("/api/cart/orders", json={"ship_country": "FR"}).json()
    paypal_order_id = shopper.post(
        f"/api/cart/orders/{order['ref']}/paypal-order", json={"version_hash": order["version_hash"]}
    ).json()["paypal_order_id"]
    paypal.approve(paypal_order_id)
    paypal.capture_outcomes.append(fake.PENDING)
    shopper.post(f"/api/cart/orders/{order['ref']}/capture", json={"paypal_order_id": paypal_order_id})
    response = deliver(webhook, event(PENDING, paypal_order_id, order["ref"]))
    assert (response.status_code, response.json()["outcome"]) == (200, "pending")
    assert stock(db, "SHELL-M")[1] == 1  # still held


# --- retryable (503) -------------------------------------------------------------------------------


def test_verification_unavailable_is_retryable_and_records_nothing(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    paypal.verify_outcomes.append(PayPalUnavailable("HTTP 500"))
    error(deliver(webhook, event(COMPLETED, paypal_order_id, ref)), 503, "webhook_retry_later")
    assert events(db) == []
    assert order_row(db, ref).status == "capturing"


def test_a_paypal_outage_while_reconciling_keeps_the_hold_and_a_redelivery_pays(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    raw = event(COMPLETED, paypal_order_id, ref)
    paypal.get_failures.append(PayPalUnavailable("ReadTimeout"))
    error(deliver(webhook, raw), 503, "webhook_retry_later")
    assert order_row(db, ref).status == "capturing"
    assert stock(db, "SHELL-M")[1] == 1  # the hold is kept
    (row,) = events(db)
    assert row.processed_at is None
    assert deliver(webhook, raw).json() == {
        "received": True,
        "event_type": COMPLETED,
        "outcome": "paid",
        "duplicate": False,
    }


@pytest.mark.parametrize(
    "settings",
    [
        {**SANDBOX, "paypal_webhook_id": ""},
        {**SANDBOX, "paypal_webhook_id": "not a webhook id!"},
        {key: value for key, value in SANDBOX.items() if not key.startswith("paypal_client")},
    ],
)
def test_without_configuration_webhooks_are_refused_retryably(
    shop_url: str, clock: FixedClock, paypal: fake.FakePayPal, settings: dict[str, str]
) -> None:
    payments_on = "paypal_client_id" in settings
    app = create_app(make_settings(shop_url, **settings), clock=clock, paypal=paypal if payments_on else None)
    client = TestClient(app)
    error(deliver(client, event(COMPLETED, "PAYPAL-ORDER-0001")), 503, "webhooks_unavailable")
    assert paypal.calls == []


# --- refused (4xx) -------------------------------------------------------------------------------


def test_an_invalid_signature_is_refused_and_nothing_is_recorded(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal, db: Session
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    paypal.verify_outcomes.append(False)
    error(deliver(webhook, event(COMPLETED, paypal_order_id, ref)), 400, "webhook_signature_invalid")
    assert events(db) == []
    assert order_row(db, ref).status == "capturing"


@pytest.mark.parametrize("missing", [name for name in HEADERS if name.startswith("PAYPAL-")])
def test_a_missing_transmission_header_is_refused_before_verification(
    webhook: TestClient, paypal: fake.FakePayPal, missing: str
) -> None:
    headers = {name: value for name, value in HEADERS.items() if name != missing}
    error(deliver(webhook, event(COMPLETED, "PAYPAL-ORDER-0001"), headers), 400, "webhook_malformed")
    assert paypal.calls_to("verify_webhook_signature") == []


def test_a_repeated_transmission_header_is_refused(webhook: TestClient, paypal: fake.FakePayPal) -> None:
    headers = [*list(HEADERS.items()), ("PAYPAL-TRANSMISSION-ID", "second")]
    response = webhook.post(URL, content=event(COMPLETED, "PAYPAL-ORDER-0001"), headers=headers)
    error(response, 400, "webhook_malformed")
    assert paypal.calls_to("verify_webhook_signature") == []


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("PAYPAL-CERT-URL", "http://api-m.sandbox.paypal.com/cert"),
        ("PAYPAL-AUTH-ALGO", "SHA256 withRSA"),
        ("PAYPAL-TRANSMISSION-SIG", "x" * 5000),
        ("PAYPAL-TRANSMISSION-ID", ""),
    ],
)
def test_a_malformed_transmission_header_is_refused(
    webhook: TestClient, paypal: fake.FakePayPal, header: str, value: str
) -> None:
    error(deliver(webhook, event(COMPLETED, "PAYPAL-ORDER-0001"), {**HEADERS, header: value}), 400, "webhook_malformed")
    assert paypal.calls_to("verify_webhook_signature") == []


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'{"id": "WH-1"}', b'{"id": "' + b"A" * 65 + b'", "event_type": "X"}', b"\xff\xfe"],
)
def test_a_malformed_body_is_refused_before_verification(
    webhook: TestClient, paypal: fake.FakePayPal, raw: bytes
) -> None:
    error(deliver(webhook, raw), 400, "webhook_malformed")
    assert paypal.calls_to("verify_webhook_signature") == []


def test_an_oversized_body_is_refused_at_the_http_layer(webhook: TestClient, paypal: fake.FakePayPal) -> None:
    padding = b" " * (MAX_EVENT_BYTES + 1)
    error(deliver(webhook, b"{}" + padding), 413, "webhook_too_large")

    def streamed() -> Iterator[bytes]:  # no Content-Length: the limit applies while reading
        for _ in range(MAX_EVENT_BYTES // 1024 + 2):
            yield b" " * 1024

    response = webhook.post(URL, content=streamed(), headers=HEADERS)
    error(response, 413, "webhook_too_large")
    assert paypal.calls == []


def test_a_body_at_the_limit_is_read(webhook: TestClient, paypal: fake.FakePayPal) -> None:
    raw = event("CHECKOUT.ORDER.APPROVED", "PAYPAL-ORDER-0001")
    raw = raw + b" " * (MAX_EVENT_BYTES - len(raw))
    response = deliver(webhook, raw)
    assert (response.status_code, response.json()["outcome"]) == (200, "ignored")


# --- settled without action (2xx) -----------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.REFUNDED", "SOMETHING.NEW"])
def test_events_atlas_does_not_act_on_are_acknowledged_as_ignored(
    webhook: TestClient, paypal: fake.FakePayPal, event_type: str
) -> None:
    response = deliver(webhook, event(event_type, "PAYPAL-ORDER-0001"))
    assert (response.status_code, response.json()["outcome"]) == (200, "ignored")
    assert paypal.calls_to("capture_order") == []


def test_an_event_for_no_atlas_order_is_acknowledged_as_unmatched(webhook: TestClient, paypal: fake.FakePayPal) -> None:
    response = deliver(webhook, event(COMPLETED, "SOMEONE-ELSES-ORDER"))
    assert (response.status_code, response.json()["outcome"]) == (200, "unmatched")
    assert paypal.calls_to("get_order") == []


def test_only_post_is_allowed(webhook: TestClient) -> None:
    assert webhook.get(URL).status_code == 405


# --- privacy ---------------------------------------------------------------------------------------


def test_logs_and_responses_never_contain_the_body_payer_or_signature(
    shopper: TestClient, webhook: TestClient, paypal: fake.FakePayPal
) -> None:
    ref, paypal_order_id = lost_capture(shopper, paypal)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        responses = [deliver(webhook, event(COMPLETED, paypal_order_id, ref))]
        paypal.verify_outcomes.append(False)
        responses.append(deliver(webhook, event(COMPLETED, paypal_order_id, ref, event_id="WH-API-2")))
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    text = stream.getvalue() + "".join(response.text for response in responses)
    for secret in (PAYER_EMAIL, TRANSMISSION.transmission_sig, TRANSMISSION.cert_url, "CAPTURE-CLAIMED", '"1.00"'):
        assert secret not in text
