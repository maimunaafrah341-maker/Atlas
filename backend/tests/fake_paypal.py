"""A deterministic in-memory PayPal (Orders v2 + Payments v2) for tests. No network.

It models the behaviour the payment rules depend on:

- an order is ``CREATED`` until the test approves it (the buyer, in PayPal's window);
- ``PayPal-Request-Id`` idempotency: repeating a create or capture with the same id returns the
  first result and never captures twice; with ``intent=CAPTURE`` an order is captured once;
- scripted outcomes for the next capture: completed, pending, declined, failed, the recoverable
  ``INSTRUMENT_DECLINED`` error (no capture created), a server error before anything happens, or
  a timeout **after** PayPal captured (the response is lost);
- tamper hooks that change what the capture record reports (amount, currency, invoice id,
  merchant, related order), for the verification tests.

Thread-safe, and it counts real capture side effects, so concurrency tests can assert that
PayPal captured exactly once.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from app.domain.payments.paypal import (
    CaptureRecord,
    CaptureRef,
    PayPalOrder,
    PayPalRejected,
    PayPalUnavailable,
)

MERCHANT_ID = "ABCDEFGH23456"

# Next-capture outcomes.
COMPLETE = "complete"
PENDING = "pending"
DECLINED = "declined"
FAILED = "failed"
INSTRUMENT_DECLINED = "instrument_declined"
SERVER_ERROR_BEFORE = "server_error_before"  # 5xx, nothing captured
TIMEOUT_AFTER_CAPTURE = "timeout_after_capture"  # captured, but the response is lost
CAPTURE_STATUS = {COMPLETE: "COMPLETED", PENDING: "PENDING", DECLINED: "DECLINED", FAILED: "FAILED"}


@dataclass
class _Order:
    id: str
    body: Mapping[str, Any]
    status: str = "CREATED"
    capture_ids: list[str] = field(default_factory=list)


class FakePayPal:
    def __init__(self, merchant_id: str = MERCHANT_ID) -> None:
        self.merchant_id = merchant_id
        self.orders: dict[str, _Order] = {}
        self.captures: dict[str, CaptureRecord] = {}
        self.capture_outcomes: list[str] = []  # consumed one per capture call; default COMPLETE
        self.create_failures: list[Exception] = []
        self.get_failures: list[Exception] = []
        self.tamper: Callable[[CaptureRecord], CaptureRecord] | None = None
        self.calls: list[tuple[str, str, str | None]] = []  # (method, resource id, request id)
        self.captures_made = 0
        self._by_request: dict[tuple[str, str], Any] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.before_capture: Callable[[], None] | None = None  # e.g. block, to hold a capture in flight

    # Test controls ---------------------------------------------------------------------------

    def approve(self, paypal_order_id: str) -> None:
        with self._lock:
            order = self.orders[paypal_order_id]
            if order.status == "CREATED":
                order.status = "APPROVED"

    def void(self, paypal_order_id: str) -> None:
        with self._lock:
            self.orders[paypal_order_id].status = "VOIDED"

    def settle(self, capture_id: str, status: str = "COMPLETED") -> None:
        """A pending capture later completes (or is declined) at PayPal."""
        with self._lock:
            self.captures[capture_id] = replace(self.captures[capture_id], status=status, status_reason=None)

    def calls_to(self, method: str) -> list[tuple[str, str, str | None]]:
        return [call for call in self.calls if call[0] == method]

    # The gateway ------------------------------------------------------------------------------

    def create_order(self, body: Mapping[str, Any], *, request_id: str) -> PayPalOrder:
        with self._lock:
            self.calls.append(("create_order", "", request_id))
            if self.create_failures:
                raise self.create_failures.pop(0)
            key = ("create", request_id)
            if key not in self._by_request:
                order = _Order(id=f"PAYPAL-ORDER-{next(self._ids):04d}", body=body)
                self.orders[order.id] = order
                self._by_request[key] = order.id
            return self._view(self.orders[self._by_request[key]])

    def capture_order(self, paypal_order_id: str, *, request_id: str) -> PayPalOrder:
        if self.before_capture is not None:
            self.before_capture()
        with self._lock:
            self.calls.append(("capture_order", paypal_order_id, request_id))
            key = ("capture", request_id)
            if key in self._by_request:  # same request id: the first result, never a second capture
                return self._view(self.orders[paypal_order_id])
            order = self.orders.get(paypal_order_id)
            if order is None:
                raise PayPalRejected(404, "RESOURCE_NOT_FOUND", debug_id="dbg-404")
            if order.status == "COMPLETED":
                raise PayPalRejected(422, "ORDER_ALREADY_CAPTURED", debug_id="dbg-captured")
            if order.status != "APPROVED":
                raise PayPalRejected(422, "ORDER_NOT_APPROVED", debug_id="dbg-not-approved")
            outcome = self.capture_outcomes.pop(0) if self.capture_outcomes else COMPLETE
            if outcome == SERVER_ERROR_BEFORE:
                raise PayPalUnavailable("HTTP 503", debug_id="dbg-503")
            if outcome == INSTRUMENT_DECLINED:
                raise PayPalRejected(422, "INSTRUMENT_DECLINED", debug_id="dbg-instrument")
            status = CAPTURE_STATUS[COMPLETE if outcome == TIMEOUT_AFTER_CAPTURE else outcome]
            capture = self._capture(order, status)
            order.capture_ids.append(capture.id)
            order.status = "COMPLETED"
            self.captures_made += 1
            self._by_request[key] = capture.id
            if outcome == TIMEOUT_AFTER_CAPTURE:
                raise PayPalUnavailable("ReadTimeout")
            return self._view(order)

    def get_order(self, paypal_order_id: str) -> PayPalOrder:
        with self._lock:
            self.calls.append(("get_order", paypal_order_id, None))
            if self.get_failures:
                raise self.get_failures.pop(0)
            order = self.orders.get(paypal_order_id)
            if order is None:
                raise PayPalRejected(404, "RESOURCE_NOT_FOUND")
            return self._view(order)

    def get_capture(self, capture_id: str) -> CaptureRecord:
        with self._lock:
            self.calls.append(("get_capture", capture_id, None))
            if self.get_failures:
                raise self.get_failures.pop(0)
            record = self.captures[capture_id]
            return self.tamper(record) if self.tamper else record

    # Internals --------------------------------------------------------------------------------

    def _capture(self, order: _Order, status: str) -> CaptureRecord:
        unit = order.body["purchase_units"][0]
        record = CaptureRecord(
            id=f"CAPTURE-{next(self._ids):04d}",
            status=status,
            status_reason="PENDING_REVIEW" if status == "PENDING" else None,
            amount_value=unit["amount"]["value"],
            currency=unit["amount"]["currency_code"],
            invoice_id=unit["invoice_id"],
            related_order_id=order.id,
            payee_merchant_id=unit.get("payee", {}).get("merchant_id"),
            debug_id="dbg-capture",
        )
        self.captures[record.id] = record
        return record

    def _view(self, order: _Order) -> PayPalOrder:
        captures = tuple(CaptureRef(cid, self.captures[cid].status) for cid in order.capture_ids)
        return PayPalOrder(order.id, order.status, captures, "dbg-order")
