"""PayPal Sandbox REST client: OAuth client credentials, Orders v2 / Payments v2 (M3.2, ADR-026) and
webhook signature verification (M3.3).

**Sandbox only (ADR-019).** The host is the constant below; there is no setting that changes
it, so this code cannot reach PayPal's live API.

The service layer talks to ``PayPalGateway`` (a Protocol), so tests inject a deterministic fake
and no automated test calls PayPal. Responses are parsed into small frozen dataclasses holding
only what verification and reconciliation need: no payer data is kept.

Errors are classified for the payment rules:

- ``PayPalUnavailable``: the outcome is **unknown** (timeout, connection failure, HTTP 5xx, an
  unreadable body). Never treated as a failure: the caller keeps the stock hold and reconciles.
- ``PayPalRejected``: PayPal answered with a 4xx and an ``issue`` code (for example
  ``INSTRUMENT_DECLINED``, ``ORDER_NOT_APPROVED``, ``ORDER_ALREADY_CAPTURED``). Each caller decides
  what that issue proves.

The client never retries a request by itself. A 401 on an API call (an expired token, so the
request was not processed) is retried exactly once with a fresh token. Nothing here logs the
client secret, the access token or a response body.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 20.0
TOKEN_REFRESH_MARGIN = 60.0  # seconds before expiry at which a cached token is replaced

_RESOURCE_ID = re.compile(r"^[A-Za-z0-9-]{1,36}$")


# --- Parsed results ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureRef:
    id: str
    status: str


@dataclass(frozen=True)
class PayPalOrder:
    id: str
    status: str
    captures: tuple[CaptureRef, ...]  # from purchase_units[].payments.captures[]
    debug_id: str | None = None


@dataclass(frozen=True)
class CaptureRecord:
    """PayPal's own capture record (GET /v2/payments/captures/{id}): the authority for payment."""

    id: str
    status: str
    status_reason: str | None
    amount_value: str | None
    currency: str | None
    invoice_id: str | None
    related_order_id: str | None
    payee_merchant_id: str | None
    debug_id: str | None = None


@dataclass(frozen=True)
class WebhookTransmission:
    """The ``PAYPAL-*`` headers of one webhook delivery, passed on unchanged to PayPal's
    verification API (M3.3). PayPal itself fetches ``cert_url``; Atlas never does."""

    auth_algo: str
    cert_url: str
    transmission_id: str
    transmission_sig: str
    transmission_time: str


# --- Errors ------------------------------------------------------------------------------------


class PayPalError(Exception):
    def __init__(self, message: str, *, debug_id: str | None = None) -> None:
        super().__init__(message)
        self.debug_id = debug_id


class PayPalUnavailable(PayPalError):
    """No answer we can rely on: the request may or may not have taken effect."""


class PayPalRejected(PayPalError):
    def __init__(self, status_code: int, issue: str | None, *, debug_id: str | None = None) -> None:
        super().__init__(
            f"PayPal rejected the request: HTTP {status_code} {issue or 'unknown issue'}", debug_id=debug_id
        )
        self.status_code = status_code
        self.issue = issue


class PayPalGateway(Protocol):
    def create_order(self, body: Mapping[str, Any], *, request_id: str) -> PayPalOrder: ...

    def capture_order(self, paypal_order_id: str, *, request_id: str) -> PayPalOrder: ...

    def get_order(self, paypal_order_id: str) -> PayPalOrder: ...

    def get_capture(self, capture_id: str) -> CaptureRecord: ...

    def verify_webhook_signature(
        self, transmission: WebhookTransmission, *, webhook_id: str, raw_event: bytes
    ) -> bool: ...


# --- Webhook verification body -----------------------------------------------------------------


def _not_json(constant: str) -> object:
    raise ValueError(f"{constant} is not JSON")


def webhook_verification_body(transmission: WebhookTransmission, *, webhook_id: str, raw_event: bytes) -> bytes:
    """The ``verify-webhook-signature`` request, with the received event bytes embedded **verbatim**.

    PayPal checks the signature over the event exactly as it sent it; parsing and re-serialising
    the event (key order, spacing, number formatting, escapes) can make a genuine event fail. So
    the event is only checked to be exactly one strict JSON object, in UTF-8, and then spliced in
    as-is. The other fields are serialised by ``json.dumps``, so no header value can add or
    replace a field.
    """
    try:
        event = json.loads(raw_event.decode("utf-8"), parse_constant=_not_json)  # BOM, NaN: refused
    except (UnicodeDecodeError, ValueError):
        raise ValueError("the webhook body is not a JSON object") from None
    if not isinstance(event, dict):
        raise ValueError("the webhook body is not a JSON object")
    fields = json.dumps(
        {
            "auth_algo": transmission.auth_algo,
            "cert_url": transmission.cert_url,
            "transmission_id": transmission.transmission_id,
            "transmission_sig": transmission.transmission_sig,
            "transmission_time": transmission.transmission_time,
            "webhook_id": webhook_id,
        },
        separators=(",", ":"),
    )
    return fields[:-1].encode("utf-8") + b',"webhook_event":' + raw_event + b"}"


# --- Money -------------------------------------------------------------------------------------


def to_paypal_value(amount_minor: int) -> str:
    """USD cents as PayPal's decimal string: 12900 -> "129.00"."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
        raise ValueError("amount must be a non-negative integer number of cents")
    return f"{amount_minor // 100}.{amount_minor % 100:02d}"


def from_paypal_value(value: str | None) -> int | None:
    """PayPal's decimal string as exact cents, or None if it isn't a plain amount with at most 2 decimals."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,12}(\.\d{1,2})?", value):
        return None
    try:
        cents = Decimal(value) * 100
    except InvalidOperation:  # pragma: no cover - the pattern already excludes it
        return None
    return int(cents) if cents == cents.to_integral_value() else None


# --- Parsing (only the fields Atlas uses) ------------------------------------------------------


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def parse_order(data: Mapping[str, Any], debug_id: str | None = None) -> PayPalOrder:
    order_id, status = _str(data.get("id")), _str(data.get("status"))
    if order_id is None or status is None:
        raise PayPalUnavailable("PayPal order response without id or status", debug_id=debug_id)
    captures = []
    units = data.get("purchase_units")
    for unit in units if isinstance(units, list) else []:
        found = _dict(_dict(unit).get("payments")).get("captures")
        for capture in found if isinstance(found, list) else []:
            capture_id, capture_status = _str(_dict(capture).get("id")), _str(_dict(capture).get("status"))
            if capture_id and capture_status:
                captures.append(CaptureRef(capture_id, capture_status))
    return PayPalOrder(order_id, status, tuple(captures), debug_id)


def parse_capture(data: Mapping[str, Any], debug_id: str | None = None) -> CaptureRecord:
    capture_id, status = _str(data.get("id")), _str(data.get("status"))
    if capture_id is None or status is None:
        raise PayPalUnavailable("PayPal capture response without id or status", debug_id=debug_id)
    amount = _dict(data.get("amount"))
    return CaptureRecord(
        id=capture_id,
        status=status,
        status_reason=_str(_dict(data.get("status_details")).get("reason")),
        amount_value=_str(amount.get("value")),
        currency=_str(amount.get("currency_code")),
        invoice_id=_str(data.get("invoice_id")),
        related_order_id=_str(_dict(_dict(data.get("supplementary_data")).get("related_ids")).get("order_id")),
        payee_merchant_id=_str(_dict(data.get("payee")).get("merchant_id")),
        debug_id=debug_id,
    )


def _resource_id(value: str) -> str:
    if not _RESOURCE_ID.match(value):
        raise ValueError("not a PayPal resource id")
    return value


# --- The Sandbox client ------------------------------------------------------------------------


class SandboxPayPalClient:
    """The real gateway, fixed to the Sandbox host. Thread-safe (one cached token, behind a lock)."""

    def __init__(self, client_id: str, client_secret: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._credentials = (client_id, client_secret)
        self._http = httpx.Client(
            base_url=SANDBOX_API_BASE,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires = 0.0

    def __repr__(self) -> str:
        return "SandboxPayPalClient()"  # never the credentials

    def close(self) -> None:
        self._http.close()

    # Orders v2 / Payments v2 -------------------------------------------------------------------

    def create_order(self, body: Mapping[str, Any], *, request_id: str) -> PayPalOrder:
        data, debug_id = self._call("POST", "/v2/checkout/orders", json=body, request_id=request_id)
        return parse_order(data, debug_id)

    def capture_order(self, paypal_order_id: str, *, request_id: str) -> PayPalOrder:
        path = f"/v2/checkout/orders/{_resource_id(paypal_order_id)}/capture"
        data, debug_id = self._call("POST", path, json={}, request_id=request_id)
        return parse_order(data, debug_id)

    def get_order(self, paypal_order_id: str) -> PayPalOrder:
        data, debug_id = self._call("GET", f"/v2/checkout/orders/{_resource_id(paypal_order_id)}")
        return parse_order(data, debug_id)

    def get_capture(self, capture_id: str) -> CaptureRecord:
        data, debug_id = self._call("GET", f"/v2/payments/captures/{_resource_id(capture_id)}")
        return parse_capture(data, debug_id)

    # Webhooks v1 (M3.3) ------------------------------------------------------------------------

    def verify_webhook_signature(self, transmission: WebhookTransmission, *, webhook_id: str, raw_event: bytes) -> bool:
        """True for HTTP 2xx with ``verification_status: SUCCESS``; False **only** for HTTP 2xx with
        ``verification_status: FAILURE``, the one answer that says the signature is invalid.

        Everything else raises ``PayPalUnavailable``, so the delivery stays retryable: a timeout
        or connection failure, any non-2xx answer (a 4xx here means our verification request,
        credentials or configuration failed, not that the webhook's signature is invalid), an
        unreadable body, or a 2xx with any other status. ValueError if ``raw_event`` isn't one
        JSON object (nothing is sent).
        """
        content = webhook_verification_body(transmission, webhook_id=webhook_id, raw_event=raw_event)
        try:
            data, debug_id = self._call("POST", "/v1/notifications/verify-webhook-signature", content=content)
        except PayPalRejected as error:  # includes a 4xx from the token request
            raise PayPalUnavailable(
                f"PayPal webhook verification request failed: HTTP {error.status_code} {error.issue or ''}".rstrip(),
                debug_id=error.debug_id,
            ) from None
        status = data.get("verification_status")
        if status == "SUCCESS":
            return True
        if status == "FAILURE":
            return False
        raise PayPalUnavailable("PayPal webhook verification without a recognised status", debug_id=debug_id)

    # Plumbing ----------------------------------------------------------------------------------

    def _access_token(self, *, refresh: bool = False) -> str:
        with self._lock:
            if not refresh and self._token and time.monotonic() < self._token_expires - TOKEN_REFRESH_MARGIN:
                return self._token
            try:
                response = self._http.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=self._credentials,
                )
            except httpx.TransportError as error:  # includes timeouts
                raise PayPalUnavailable(f"PayPal token request failed: {type(error).__name__}") from None
            debug_id = response.headers.get("paypal-debug-id")
            if response.status_code >= 500:
                raise PayPalUnavailable(f"PayPal token request: HTTP {response.status_code}", debug_id=debug_id)
            if response.status_code != 200:
                raise PayPalRejected(response.status_code, _error_issue(response), debug_id=debug_id)
            data = _json(response)
            token, expires_in = data.get("access_token"), data.get("expires_in")
            if not isinstance(token, str) or not token or not isinstance(expires_in, int):
                raise PayPalUnavailable("PayPal token response without a token", debug_id=debug_id)
            self._token, self._token_expires = token, time.monotonic() + expires_in
            return token

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        request_id: str | None = None,
    ) -> tuple[Mapping[str, Any], str | None]:
        """``json`` is serialised by httpx; ``content`` is an already-built JSON body, sent byte for byte."""
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._access_token(refresh=attempt == 2)}"}
            if json is not None:
                headers["Content-Type"] = "application/json"
                headers["Prefer"] = "return=representation"
            elif content is not None:
                headers["Content-Type"] = "application/json"
            if request_id is not None:
                headers["PayPal-Request-Id"] = request_id
            try:
                response = self._http.request(method, path, json=json, content=content, headers=headers)
            except httpx.TransportError as error:  # includes timeouts: outcome unknown
                raise PayPalUnavailable(f"PayPal {method} {path} failed: {type(error).__name__}") from None
            debug_id = response.headers.get("paypal-debug-id")
            if response.status_code == 401 and attempt == 1:
                continue  # not processed: retry once with a fresh token
            if response.status_code >= 500:
                raise PayPalUnavailable(f"PayPal {method} {path}: HTTP {response.status_code}", debug_id=debug_id)
            if response.status_code >= 400:
                raise PayPalRejected(
                    response.status_code, _error_issue(response), debug_id=_debug(response) or debug_id
                )
            data = _json(response)
            if not data:
                raise PayPalUnavailable(f"PayPal {method} {path}: unreadable body", debug_id=debug_id)
            return data, debug_id
        raise PayPalRejected(401, "AUTHENTICATION_FAILURE")  # pragma: no cover - loop always returns


def _json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, Mapping) else {}


def _debug(response: httpx.Response) -> str | None:
    return _str(_json(response).get("debug_id"))


def _error_issue(response: httpx.Response) -> str | None:
    """PayPal's machine-readable issue: details[0].issue, else the error name (or OAuth ``error``)."""
    data = _json(response)
    details = data.get("details")
    if isinstance(details, list) and details:
        issue = _str(_dict(details[0]).get("issue"))
        if issue:
            return issue
    return _str(data.get("name")) or _str(data.get("error"))
