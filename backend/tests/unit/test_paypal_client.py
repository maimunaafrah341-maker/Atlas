"""The Sandbox PayPal client against a mock transport (no network), money conversion, and the
capture verification checks (M3.2, ADR-026)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.domain.orders.service import OrderLineView, OrderView
from app.domain.payments.paypal import (
    SANDBOX_API_BASE,
    CaptureRecord,
    PayPalRejected,
    PayPalUnavailable,
    SandboxPayPalClient,
    from_paypal_value,
    to_paypal_value,
)
from app.domain.payments.verify import Expected, capture_problem, order_request

Handler = Callable[[httpx.Request], httpx.Response]
TOKEN = {"access_token": "access-token-value", "token_type": "Bearer", "expires_in": 32400}
CAPTURE = {
    "id": "CAP123",
    "status": "COMPLETED",
    "amount": {"currency_code": "USD", "value": "176.00"},
    "invoice_id": "a" * 32,
    "payee": {"email_address": "merchant@example.com", "merchant_id": "ABCDEFGH23456"},
    "supplementary_data": {"related_ids": {"order_id": "ORDER123"}},
    "seller_receivable_breakdown": {"gross_amount": {"currency_code": "USD", "value": "176.00"}},
}


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(recorder: Handler) -> SandboxPayPalClient:
    return SandboxPayPalClient("client-id", "client-secret", transport=httpx.MockTransport(recorder))


def test_requests_go_to_the_sandbox_with_oauth_and_the_request_id() -> None:
    recorder = Recorder(
        httpx.Response(201, json={"id": "ORDER123", "status": "CREATED"}, headers={"paypal-debug-id": "d1"})
    )
    order = client(recorder).create_order({"intent": "CAPTURE"}, request_id="atlas-x-create")

    assert (order.id, order.status, order.debug_id) == ("ORDER123", "CREATED", "d1")
    token_request, create = recorder.requests
    assert str(token_request.url) == f"{SANDBOX_API_BASE}/v1/oauth2/token"
    assert token_request.headers["authorization"] == "Basic " + base64.b64encode(b"client-id:client-secret").decode()
    assert token_request.content == b"grant_type=client_credentials"
    assert str(create.url) == f"{SANDBOX_API_BASE}/v2/checkout/orders"
    assert create.headers["authorization"] == "Bearer access-token-value"
    assert create.headers["paypal-request-id"] == "atlas-x-create"
    assert create.headers["prefer"] == "return=representation"
    assert json.loads(create.content) == {"intent": "CAPTURE"}


def test_the_token_is_cached_and_refreshed_once_on_401() -> None:
    order = {"id": "ORDER123", "status": "APPROVED"}
    recorder = Recorder(httpx.Response(200, json=order), httpx.Response(401, json={}), httpx.Response(200, json=order))
    paypal = client(recorder)
    paypal.get_order("ORDER123")
    paypal.get_order("ORDER123")  # 401: a fresh token, then the same request again
    paths = [request.url.path for request in recorder.requests]
    assert paths == [
        "/v1/oauth2/token",
        "/v2/checkout/orders/ORDER123",
        "/v2/checkout/orders/ORDER123",
        "/v1/oauth2/token",
        "/v2/checkout/orders/ORDER123",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, text="not json"),
    ],
)
def test_unknown_outcomes_are_unavailable_never_rejected(failure: httpx.Response | Exception) -> None:
    with pytest.raises(PayPalUnavailable):
        client(Recorder(failure)).capture_order("ORDER123", request_id="atlas-x-capture-1")


def test_rejections_carry_the_issue_and_debug_id() -> None:
    body = {
        "name": "UNPROCESSABLE_ENTITY",
        "details": [{"issue": "INSTRUMENT_DECLINED", "description": "declined"}],
        "debug_id": "dbg-1",
    }
    with pytest.raises(PayPalRejected) as raised:
        client(Recorder(httpx.Response(422, json=body))).capture_order("ORDER123", request_id="atlas-x-capture-1")
    assert (raised.value.status_code, raised.value.issue, raised.value.debug_id) == (
        422,
        "INSTRUMENT_DECLINED",
        "dbg-1",
    )


def test_capture_record_keeps_only_verification_fields() -> None:
    record = client(Recorder(httpx.Response(200, json=CAPTURE))).get_capture("CAP123")
    assert record == CaptureRecord(
        id="CAP123",
        status="COMPLETED",
        status_reason=None,
        amount_value="176.00",
        currency="USD",
        invoice_id="a" * 32,
        related_order_id="ORDER123",
        payee_merchant_id="ABCDEFGH23456",
        debug_id=None,
    )
    assert "merchant@example.com" not in repr(record)


def test_order_captures_are_read_from_purchase_units() -> None:
    body = {
        "id": "ORDER123",
        "status": "COMPLETED",
        "payer": {"email_address": "buyer@example.com", "name": {"given_name": "Ana"}},
        "purchase_units": [{"payments": {"captures": [{"id": "CAP123", "status": "PENDING"}]}}],
    }
    order = client(Recorder(httpx.Response(201, json=body))).capture_order("ORDER123", request_id="r")
    assert [(c.id, c.status) for c in order.captures] == [("CAP123", "PENDING")]
    assert "buyer@example.com" not in repr(order)


@pytest.mark.parametrize("bad", ["../oauth2/token", "ORDER 1", "", "x" * 37])
def test_resource_ids_cannot_change_the_path(bad: str) -> None:
    with pytest.raises(ValueError, match="resource id"):
        client(Recorder()).get_order(bad)


def test_the_client_never_shows_its_credentials() -> None:
    assert "client-secret" not in repr(client(Recorder()))


# --- money -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("cents", "text"), [(0, "0.00"), (5, "0.05"), (12900, "129.00"), (17601, "176.01")])
def test_money_round_trips_exactly(cents: int, text: str) -> None:
    assert to_paypal_value(cents) == text
    assert from_paypal_value(text) == cents


@pytest.mark.parametrize("bad", ["176.001", "-1.00", "1e3", "", None, "176,00", " 1.00"])
def test_unusual_amounts_are_not_amounts(bad: Any) -> None:
    assert from_paypal_value(bad) is None


@pytest.mark.parametrize("value", ["176", "176.0"])
def test_short_decimals_are_exact(value: str) -> None:
    assert from_paypal_value(value) == 17600


# --- verification ------------------------------------------------------------------------------

EXPECTED = Expected(paypal_order_id="ORDER123", invoice_id="a" * 32, merchant_id="ABCDEFGH23456", total_minor=17600)
GOOD = CaptureRecord("CAP123", "COMPLETED", None, "176.00", "USD", "a" * 32, "ORDER123", "ABCDEFGH23456")


def test_a_matching_capture_has_no_problem() -> None:
    assert capture_problem(GOOD, EXPECTED) is None


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"related_order_id": "OTHER"}, "order_mismatch"),
        ({"related_order_id": None}, "order_mismatch"),
        ({"invoice_id": "b" * 32}, "reference_mismatch"),
        ({"invoice_id": None}, "reference_mismatch"),
        ({"payee_merchant_id": "ZZZZZZZZZZZZZ"}, "payee_mismatch"),
        ({"payee_merchant_id": None}, "payee_mismatch"),  # fail closed: missing is not a pass
        ({"currency": "EUR"}, "currency_mismatch"),
        ({"currency": None}, "currency_mismatch"),
        ({"amount_value": "175.99"}, "amount_mismatch"),
        ({"amount_value": "176.001"}, "amount_mismatch"),
        ({"amount_value": None}, "amount_mismatch"),
    ],
)
def test_every_mismatch_is_named(change: dict[str, Any], problem: str) -> None:
    assert capture_problem(replace(GOOD, **change), EXPECTED) == problem


def test_the_order_request_is_the_snapshot() -> None:
    now = datetime(2026, 11, 10, tzinfo=UTC)
    order = OrderView(
        public_ref="a" * 32,
        status="approved",
        version_hash="0" * 64,
        currency="USD",
        subtotal_minor=16100,
        shipping_minor=1500,
        total_minor=17600,
        ship_country="FR",
        shipping_zone="europe",
        lang="es",
        hold_expires_at=now,
        created_at=now,
        status_changed_at=now,
        lines=(
            OrderLineView("SHELL-M", "rain-shell", "Rain Shell", "M", "navy", 12900, 1, 12900),
            OrderLineView("SOCK-M", "trail-sock", "Trail Sock", "M", "grey", 1600, 2, 3200),
        ),
    )
    body = order_request(order, merchant_id="ABCDEFGH23456")
    unit = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert (unit["reference_id"], unit["custom_id"], unit["invoice_id"]) == ("a" * 32,) * 3
    assert unit["payee"] == {"merchant_id": "ABCDEFGH23456"}
    assert unit["amount"] == {
        "currency_code": "USD",
        "value": "176.00",
        "breakdown": {
            "item_total": {"currency_code": "USD", "value": "161.00"},
            "shipping": {"currency_code": "USD", "value": "15.00"},
        },
    }
    assert [(i["sku"], i["quantity"], i["unit_amount"]["value"]) for i in unit["items"]] == [
        ("SHELL-M", "1", "129.00"),
        ("SOCK-M", "2", "16.00"),
    ]
    assert body["payment_source"]["paypal"]["experience_context"]["shipping_preference"] == "NO_SHIPPING"
    with pytest.raises(ValueError, match="add up"):
        order_request(replace(order, total_minor=1), merchant_id="ABCDEFGH23456")
