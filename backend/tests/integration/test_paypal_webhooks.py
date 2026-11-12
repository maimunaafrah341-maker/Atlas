"""PayPal webhook processing against the fake PayPal (M3.3). SQLite and PostgreSQL.

The event body is a signal only: every test that changes an order shows the change comes from
PayPal's own (fake) state, and the inventory invariants are checked after each test (``s``).
Concurrent deliveries are in test_payment_concurrency.py (PostgreSQL).
"""

# The payment service tests' fixtures (factory, s, clock, paypal) are imported so pytest finds
# them here; test parameters with the same names are how fixtures are requested.
# ruff: noqa: F811

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import FixedClock
from app.domain.orders.models import Order
from app.domain.payments.models import PayPalWebhookEvent
from app.domain.payments.paypal import PayPalRejected, PayPalUnavailable, WebhookTransmission
from app.domain.payments.service import STALE_CAPTURE_AFTER
from app.domain.payments.webhooks import (
    COMPLETED,
    DENIED,
    MAX_EVENT_BYTES,
    PENDING,
    WebhookMalformed,
    WebhookResult,
    WebhookRetryLater,
    WebhookSignatureInvalid,
    WebhooksUnavailable,
    handle_webhook,
)
from tests import fake_paypal as fake
from tests.integration.test_payment_service import (  # noqa: F401  (fixtures: factory, s, clock, paypal)
    MERCHANT,
    Checkout,
    bag_lines,
    clock,
    factory,
    paypal,
    s,
    sales,
)
from tests.order_support import look, stock

TRANSMISSION = WebhookTransmission(
    auth_algo="SHA256withRSA",
    cert_url="https://api-m.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-1d93a270",
    transmission_id="69cd13f0-d67a-11e5-baa3-778b53f4ae55",
    transmission_sig="bG1JOTVKeDNZOW5oUjVTSldsSFZJV3BnNEFnRms3bjliQ0hTUnhicmQ4QTl6cmhkdTJyTXlGcm16K1pqaDNzMw==",
    transmission_time="2026-11-12T10:42:13Z",
)
PAYER_EMAIL = "sb-buyer-4471@personal.example.com"


class Delivery:
    """PayPal delivering events about one checkout (or about nothing Atlas knows)."""

    def __init__(self, s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
        self.s, self.clock, self.paypal = s, clock, paypal

    def event(
        self,
        event_type: str,
        *,
        event_id: str = "WH-1",
        c: Checkout | None = None,
        paypal_order_id: str | None = None,
        invoice_id: str | None = None,
        names_paypal_order: bool = True,
        claims: str = "COMPLETED",
        create_time: str = "2026-11-12T10:42:13.000Z",
    ) -> bytes:
        """A PayPal-shaped event. Its resource deliberately claims things PayPal may not show."""
        resource: dict[str, Any] = {
            "id": "CAPTURE-CLAIMED",
            "status": claims,
            "amount": {"currency_code": "USD", "value": "1.00"},  # never what the order costs
            "payee": {"email_address": "merchant@example.com", "merchant_id": MERCHANT},
            "payer": {"email_address": PAYER_EMAIL, "name": {"given_name": "John", "surname": "Doe"}},
        }
        if names_paypal_order:
            related = {"order_id": paypal_order_id or (c.paypal_order_id if c else "UNKNOWN-ORDER")}
            resource["supplementary_data"] = {"related_ids": related}
        if invoice_id or c:
            resource["invoice_id"] = invoice_id or (c.ref if c else None)
        body = {
            "id": event_id,
            "create_time": create_time,
            "resource_type": "capture",
            "event_type": event_type,
            "summary": f"Payment {claims.lower()} for $ 1.00 USD",
            "resource": resource,
        }
        return json.dumps(body).encode("utf-8")

    def __call__(self, raw: bytes, *, transmission: WebhookTransmission = TRANSMISSION) -> WebhookResult:
        return handle_webhook(
            self.s,
            raw,
            transmission,
            gateway=self.paypal,
            webhook_id=fake.WEBHOOK_ID,
            merchant_id=MERCHANT,
            clock=self.clock,
        )

    def row(self, event_id: str = "WH-1") -> PayPalWebhookEvent | None:
        def read() -> PayPalWebhookEvent | None:
            row = self.s.execute(
                select(PayPalWebhookEvent).where(PayPalWebhookEvent.event_id == event_id)
            ).scalar_one_or_none()
            if row is not None:
                self.s.expunge(row)
            return row

        return look(self.s, read)

    def rows(self) -> int:
        return look(self.s, lambda: len(self.s.execute(select(PayPalWebhookEvent.id)).all()))

    def paypal_reads(self) -> int:
        return len(self.paypal.calls_to("get_order")) + len(self.paypal.calls_to("get_capture"))


@pytest.fixture
def deliver(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> Delivery:
    return Delivery(s, clock, paypal)


def lost_capture(s: Session, clock: FixedClock, paypal: fake.FakePayPal, outcome: str = fake.COMPLETE) -> Checkout:
    """PayPal captured, but Atlas never saw the answer: the order is still ``capturing``."""
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.TIMEOUT_AFTER_CAPTURE if outcome == fake.COMPLETE else outcome)
    result = c.capture()
    if outcome == fake.COMPLETE:
        assert (result.result, c.status) == ("processing", "capturing")
    return c


def only_capture(paypal: fake.FakePayPal) -> str:
    [capture_id] = paypal.captures
    return capture_id


# --- completed --------------------------------------------------------------------------------


def test_a_completed_event_after_a_lost_capture_response_finalizes_exactly_once(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    before = stock(s, "A-S")

    result = deliver(deliver.event(COMPLETED, c=c))

    assert result == WebhookResult("WH-1", COMPLETED, "paid", duplicate=False)
    assert (c.status, c.hold) == ("paid", "committed")
    assert sales(s) == 1
    assert stock(s, "A-S") == (before[0] - 1, before[1] - 1)
    assert bag_lines(s, c.cart_id) == 0
    assert c.evidence() == [("COMPLETED", "verified", None)]  # PayPal's record, not the body's 1.00
    row = deliver.row()
    assert row is not None
    assert (row.outcome, row.deliveries, row.processed_at) == ("paid", 1, clock.now())
    assert row.order_id == c.field(Order.id)
    assert len(paypal.calls_to("capture_order")) == 1  # the webhook never captured

    # The browser's retried capture and a redelivery of the same event change nothing.
    reads = deliver.paypal_reads()
    again = deliver(deliver.event(COMPLETED, c=c))
    assert again == WebhookResult("WH-1", COMPLETED, "paid", duplicate=True)
    assert deliver.paypal_reads() == reads  # a processed event isn't reconciled again
    assert c.capture().result == "paid"
    assert sales(s) == 1
    assert c.evidence() == [("COMPLETED", "verified", None)]
    row = deliver.row()
    assert row is not None
    assert row.deliveries == 2


def test_without_a_paypal_order_id_the_atlas_reference_matches(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    assert deliver(deliver.event(COMPLETED, invoice_id=c.ref, names_paypal_order=False)).outcome == "paid"
    assert (c.status, sales(s)) == ("paid", 1)
    row = deliver.row()
    assert row is not None
    assert row.order_id == c.field(Order.id)
    # PayPal was asked about the order's STORED PayPal order.
    assert {call[1] for call in deliver.paypal.calls_to("get_order")} == {c.paypal_order_id}


@pytest.mark.parametrize("named", ["SOMEONE-ELSES-ORDER", "not a paypal id!"])
def test_an_unknown_paypal_order_id_is_unmatched_even_with_a_valid_reference(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery, named: str
) -> None:
    c = lost_capture(s, clock, paypal)
    reads = deliver.paypal_reads()
    result = deliver(deliver.event(COMPLETED, paypal_order_id=named, invoice_id=c.ref))
    assert result.outcome == "unmatched"  # no fallback to the reference
    assert deliver.paypal_reads() == reads
    row = deliver.row()
    assert row is not None
    assert (row.order_id, row.outcome) == (None, "unmatched")
    assert row.processed_at is not None
    assert (c.status, c.hold, sales(s)) == ("capturing", "order_hold", 0)


def test_identifiers_pointing_at_two_different_orders_are_unmatched(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    first = lost_capture(s, clock, paypal)
    second = Checkout(s, clock, paypal, {"A-M": 1}).approved()
    reads = deliver.paypal_reads()
    result = deliver(deliver.event(COMPLETED, paypal_order_id=first.paypal_order_id, invoice_id=second.ref))
    assert result.outcome == "unmatched"
    assert deliver.paypal_reads() == reads  # neither order was reconciled
    row = deliver.row()
    assert row is not None
    assert (row.order_id, row.outcome) == (None, "unmatched")  # nor associated
    assert (first.status, first.hold) == ("capturing", "order_hold")
    assert (second.status, second.hold) == ("awaiting_payment", "order_hold")
    assert sales(s) == 0


def test_a_reference_matching_the_named_paypal_order_is_consistent(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    unknown_reference = deliver.event(COMPLETED, c=c, invoice_id="e" * 32, event_id="WH-2")
    assert deliver(deliver.event(COMPLETED, c=c)).outcome == "paid"  # both identify the same order
    # A reference that belongs to no order isn't a conflict: the PayPal order id decides.
    assert deliver(unknown_reference).outcome == "paid"


def test_the_event_body_never_overrides_paypal(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    """The body says COMPLETED; PayPal's capture record says DECLINED. PayPal wins, and that is final."""
    c = lost_capture(s, clock, paypal)
    paypal.settle(only_capture(paypal), "DECLINED")
    on_hand = stock(s, "A-S")
    result = deliver(deliver.event(COMPLETED, c=c, claims="COMPLETED"))
    assert result.outcome == "failed"
    assert (c.status, c.hold) == ("payment_failed", "released")
    assert stock(s, "A-S") == (on_hand[0], on_hand[1] - 1)  # the hold went back; nothing was sold
    assert sales(s) == 0
    row = deliver.row()
    assert row is not None
    assert row.processed_at is not None


def test_a_completed_event_with_no_capture_at_paypal_never_captures_and_waits(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    """Approved at PayPal, nothing captured: the webhook must not start a capture."""
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(COMPLETED, c=c))
    assert paypal.calls_to("capture_order") == []
    assert paypal.captures_made == 0
    assert (c.status, c.hold) == ("awaiting_payment", "order_hold")
    row = deliver.row()
    assert row is not None
    assert (row.processed_at, row.outcome) == (None, "awaiting_payment")  # still retryable


def test_a_stale_capture_claim_is_not_retried_by_a_webhook(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.SERVER_ERROR_BEFORE)  # nothing captured; the claim stays
    c.capture()
    assert c.status == "capturing"
    clock.advance(STALE_CAPTURE_AFTER + timedelta(seconds=1))  # a customer reconcile would retry now
    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(COMPLETED, c=c))
    assert len(paypal.calls_to("capture_order")) == 1  # only the customer's original attempt
    assert (c.status, c.hold) == ("capturing", "order_hold")


# --- pending and lagging PayPal state ---------------------------------------------------------


def test_a_pending_event_keeps_the_hold(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.PENDING)
    assert c.capture().result == "pending"
    held = stock(s, "A-S")

    assert deliver(deliver.event(PENDING, c=c, claims="PENDING")).outcome == "pending"
    assert (c.status, c.hold) == ("payment_pending", "order_hold")
    assert stock(s, "A-S") == held
    row = deliver.row()
    assert row is not None
    assert row.processed_at is not None  # PayPal shows the capture pending: caught up

    paypal.settle(only_capture(paypal), "COMPLETED")
    assert deliver(deliver.event(COMPLETED, c=c, event_id="WH-2")).outcome == "paid"
    assert (c.status, c.hold, sales(s)) == ("paid", "committed", 1)


def test_a_completed_event_waits_while_paypal_still_shows_pending(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.PENDING)
    c.capture()
    held = stock(s, "A-S")

    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(COMPLETED, c=c))  # PayPal hasn't caught up with its own event yet
    assert (c.status, c.hold) == ("payment_pending", "order_hold")
    assert stock(s, "A-S") == held
    row = deliver.row()
    assert row is not None
    assert (row.processed_at, row.outcome, row.deliveries) == (None, "pending", 1)

    paypal.settle(only_capture(paypal), "COMPLETED")
    clock.advance(timedelta(minutes=5))
    assert deliver(deliver.event(COMPLETED, c=c)).outcome == "paid"  # PayPal's redelivery
    row = deliver.row()
    assert row is not None
    assert (row.processed_at, row.outcome, row.deliveries) == (clock.now(), "paid", 2)
    assert sales(s) == 1


# --- denied -------------------------------------------------------------------------------------


def test_a_denied_event_releases_stock_only_when_paypal_confirms_the_decline(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.PENDING)
    c.capture()
    held = stock(s, "A-S")

    # PayPal still shows PENDING: the denial isn't confirmed, so nothing is released.
    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(DENIED, c=c, claims="DECLINED"))
    assert (c.status, c.hold) == ("payment_pending", "order_hold")
    assert stock(s, "A-S") == held

    paypal.settle(only_capture(paypal), "DECLINED")
    assert deliver(deliver.event(DENIED, c=c, claims="DECLINED")).outcome == "failed"
    assert (c.status, c.hold) == ("payment_failed", "released")
    assert stock(s, "A-S") == (held[0], held[1] - 1)
    assert sales(s) == 0


def test_a_denied_event_for_a_payment_paypal_shows_completed_pays_the_order(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    assert deliver(deliver.event(DENIED, c=c, claims="DECLINED")).outcome == "paid"
    assert (c.status, c.hold, sales(s)) == ("paid", "committed", 1)


def test_late_and_out_of_order_events_leave_a_paid_order_alone(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    assert c.capture().result == "paid"
    for number, (event_type, claims) in enumerate([(PENDING, "PENDING"), (DENIED, "DECLINED"), (COMPLETED, "X")]):
        result = deliver(deliver.event(event_type, c=c, claims=claims, event_id=f"WH-late-{number}"))
        assert result.outcome == "paid"
    assert (c.status, c.hold, sales(s)) == ("paid", "committed", 1)
    assert c.evidence() == [("COMPLETED", "verified", None)]


def test_an_old_event_is_still_accepted(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    assert deliver(deliver.event(COMPLETED, c=c, create_time="2026-11-09T10:42:13.000Z")).outcome == "paid"


# --- PayPal unavailable ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure", [PayPalUnavailable("ReadTimeout"), PayPalRejected(404, "RESOURCE_NOT_FOUND", debug_id="dbg-1")]
)
def test_paypal_unavailable_leaves_the_event_retryable_and_the_hold_kept(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery, failure: Exception
) -> None:
    c = lost_capture(s, clock, paypal)
    paypal.get_failures.append(failure)
    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(COMPLETED, c=c))
    assert (c.status, c.hold) == ("capturing", "order_hold")
    row = deliver.row()
    assert row is not None
    assert (row.processed_at, row.outcome) == (None, None)

    assert deliver(deliver.event(COMPLETED, c=c)).outcome == "paid"  # the redelivery succeeds
    assert sales(s) == 1


def test_an_unreadable_capture_record_leaves_the_event_retryable(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    original = paypal.get_capture

    def unavailable(capture_id: str) -> Any:
        raise PayPalUnavailable("ReadTimeout")

    paypal.get_capture = unavailable  # type: ignore[method-assign]  # the PayPal order is readable; this isn't
    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(COMPLETED, c=c))
    assert (c.status, c.hold) == ("capturing", "order_hold")
    paypal.get_capture = original  # type: ignore[method-assign]
    assert deliver(deliver.event(COMPLETED, c=c)).outcome == "paid"


# --- verification and malformed deliveries --------------------------------------------------------


@pytest.mark.parametrize("outcome", [PayPalUnavailable("HTTP 503"), PayPalUnavailable("HTTP 401")])
def test_unavailable_verification_records_nothing_and_is_retryable(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery, outcome: Exception
) -> None:
    c = lost_capture(s, clock, paypal)
    paypal.verify_outcomes.append(outcome)
    with pytest.raises(WebhookRetryLater):
        deliver(deliver.event(COMPLETED, c=c))
    assert deliver.rows() == 0
    assert c.status == "capturing"


def test_a_rejected_signature_records_nothing_and_reconciles_nothing(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    reads = deliver.paypal_reads()
    paypal.verify_outcomes.append(False)  # HTTP 200, verification_status FAILURE
    with pytest.raises(WebhookSignatureInvalid):
        deliver(deliver.event(COMPLETED, c=c))
    assert deliver.rows() == 0
    assert deliver.paypal_reads() == reads
    assert c.status == "capturing"


def test_the_raw_bytes_are_what_paypal_verifies(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    raw = b"  " + deliver.event(COMPLETED, c=c).replace(b", ", b" ,  ") + b"\n"  # spacing json.dumps wouldn't keep
    deliver(raw)
    assert paypal.verified_events == [raw]


def test_not_configured_is_unavailable(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    with pytest.raises(WebhooksUnavailable):
        handle_webhook(s, b"{}", TRANSMISSION, gateway=paypal, webhook_id="", merchant_id=MERCHANT, clock=clock)
    assert paypal.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"not json",
        b'{"id":"WH-1"}',  # no event type
        b'{"event_type":"PAYMENT.CAPTURE.COMPLETED"}',  # no id
        b'{"id":"WH 1","event_type":"PAYMENT.CAPTURE.COMPLETED"}',
        b'{"id":"' + b"A" * 65 + b'","event_type":"PAYMENT.CAPTURE.COMPLETED"}',  # longer than the column: refused
        b'{"id":1,"event_type":"PAYMENT.CAPTURE.COMPLETED"}',
        b'{"id":"WH-1","event_type":"PAYMENT CAPTURE"}',
        b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED","x":NaN}',
        b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED","pad":"' + b"x" * MAX_EVENT_BYTES + b'"}',
    ],
)
def test_a_malformed_event_is_refused_before_verification(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery, raw: bytes
) -> None:
    with pytest.raises(WebhookMalformed):
        deliver(raw)
    assert paypal.calls == []
    assert deliver.rows() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"auth_algo": ""},
        {"cert_url": "http://api-m.sandbox.paypal.com/cert"},  # not https
        {"cert_url": "https://x/" + "a" * 600},
        {"transmission_id": "has space"},
        {"transmission_sig": "a" * 4097},
        {"transmission_time": "2026-11-12\n10:42"},
    ],
)
def test_malformed_transmission_headers_are_refused_before_verification(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery, change: dict[str, str]
) -> None:
    from dataclasses import replace

    with pytest.raises(WebhookMalformed):
        deliver(b'{"id":"WH-1","event_type":"X"}', transmission=replace(TRANSMISSION, **change))
    assert paypal.calls == []


# --- duplicates, unmatched, unknown and out-of-scope events -----------------------------------------


def test_every_verified_delivery_is_counted(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    first = clock.now()
    deliver(deliver.event(COMPLETED, c=c))
    for _ in range(3):
        clock.advance(timedelta(minutes=1))
        assert deliver(deliver.event(COMPLETED, c=c)).duplicate
    row = deliver.row()
    assert row is not None
    assert (row.deliveries, row.received_at, row.last_received_at, row.processed_at) == (
        4,
        first,
        clock.now(),
        first,
    )
    assert sales(s) == 1


def test_an_event_for_no_atlas_order_is_unmatched_and_settled(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery
) -> None:
    c = lost_capture(s, clock, paypal)
    reads = deliver.paypal_reads()
    result = deliver(deliver.event(COMPLETED, paypal_order_id="SOMEONE-ELSES-ORDER", invoice_id="f" * 32))
    assert result.outcome == "unmatched"
    assert deliver.paypal_reads() == reads  # nothing to reconcile
    row = deliver.row()
    assert row is not None
    assert (row.order_id, row.outcome) == (None, "unmatched")
    assert row.processed_at is not None
    assert (c.status, c.hold) == ("capturing", "order_hold")  # untouched, still held


@pytest.mark.parametrize(
    ("event_type", "level"),
    [
        ("CHECKOUT.ORDER.APPROVED", logging.INFO),  # not subscribed in M3.3; never auto-captures
        ("PAYMENT.CAPTURE.REFUNDED", logging.WARNING),  # future work: not a payment
        ("PAYMENT.CAPTURE.REVERSED", logging.WARNING),
        ("SOMETHING.NEW.HAPPENED", logging.INFO),
    ],
)
def test_events_atlas_does_not_act_on_are_ignored_and_auditable(
    s: Session,
    clock: FixedClock,
    paypal: fake.FakePayPal,
    deliver: Delivery,
    caplog: pytest.LogCaptureFixture,
    event_type: str,
    level: int,
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    assert c.capture().result == "paid"
    reads = deliver.paypal_reads()
    with caplog.at_level(logging.INFO, logger="app.domain.payments.webhooks"):
        result = deliver(deliver.event(event_type, c=c, claims="REFUNDED"))
    assert result.outcome == "ignored"
    assert deliver.paypal_reads() == reads
    assert len(paypal.calls_to("capture_order")) == 1
    assert (c.status, c.hold, sales(s)) == ("paid", "committed", 1)  # a refund isn't applied, nor "paid" again
    row = deliver.row()
    assert row is not None
    assert (row.event_type, row.outcome, row.order_id) == (event_type, "ignored", None)
    assert row.processed_at is not None
    assert [record.levelno for record in caplog.records if record.name == "app.domain.payments.webhooks"] == [level]


def test_nothing_from_the_body_or_headers_is_stored_or_logged(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, deliver: Delivery, caplog: pytest.LogCaptureFixture
) -> None:
    c = lost_capture(s, clock, paypal)
    with caplog.at_level(logging.DEBUG):
        deliver(deliver.event(COMPLETED, c=c))
    row = deliver.row()
    assert row is not None
    stored = " ".join(str(value) for value in vars(row).values())
    logged = caplog.text + " ".join(str(vars(record)) for record in caplog.records)
    secrets = (
        PAYER_EMAIL,
        "John",
        "merchant@example.com",
        "CAPTURE-CLAIMED",
        TRANSMISSION.transmission_sig,
        TRANSMISSION.cert_url,
        TRANSMISSION.transmission_id,
        "Payment completed for",
    )
    for secret in secrets:
        assert secret not in stored
        assert secret not in logged
