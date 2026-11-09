"""Payment service against the fake PayPal (M3.2, ADR-026). SQLite and PostgreSQL.

Every test ends with the inventory invariants (``s`` fixture), including: a paid order has
verified, completed payment evidence; stock is committed exactly once.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.db import create_db_engine, session_factory
from app.domain.cart.service import set_quantity
from app.domain.inventory.models import InventoryMovement
from app.domain.orders.holds import CheckoutInProgress, expire_order_if_due
from app.domain.orders.models import Order
from app.domain.orders.service import OrderNotFound, PaymentInProgress, cancel_order, create_order
from app.domain.payments import service as payments
from app.domain.payments.models import Payment
from app.domain.payments.paypal import CaptureRecord, PayPalUnavailable
from app.domain.payments.service import (
    CAPTURE_MARGIN,
    RESOLUTION_WINDOW,
    STALE_CAPTURE_AFTER,
    HoldExpiring,
    OrderChanged,
    OrderNotPayable,
    PaymentProviderUnavailable,
    PayPalOrderMismatch,
    capture_payment,
    reconcile_by_reference,
    reconcile_payment,
    start_paypal_checkout,
)
from app.domain.reservations.service import reserve_cart
from tests import fake_paypal as fake
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.order_support import HOLD, add_shipping, look, order_row, reasons, stock
from tests.reservation_builders import NOW, TTL

MERCHANT = fake.MERCHANT_ID


@pytest.fixture
def factory(migrated_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(migrated_database_url)
    factory = session_factory(engine)
    with factory() as session:
        make_product(
            session,
            "shell",
            variants=[V(sku="A-S", size="S", price_minor=12900, on_hand=5), V(sku="A-M", price_minor=13900, on_hand=2)],
        )
        add_shipping(session)
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def s(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
        session.rollback()
        assert_inventory_invariants(session)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def paypal() -> fake.FakePayPal:
    return fake.FakePayPal()


class Checkout:
    """One customer's order, driven through the payment service."""

    def __init__(self, s: Session, clock: FixedClock, paypal: fake.FakePayPal, lines: dict[str, int]) -> None:
        self.s, self.clock, self.paypal = s, clock, paypal
        cart_id: int | None = None
        for sku, quantity in lines.items():
            cart_id = set_quantity(s, cart_id, sku, quantity, clock=clock).cart.cart_id
        assert cart_id is not None
        self.cart_id = cart_id
        reserve_cart(s, cart_id, clock=clock, ttl=TTL)
        order = create_order(s, cart_id, ship_country="FR", lang="en", clock=clock, hold=HOLD).order
        self.ref, self.version_hash, self.total = order.public_ref, order.version_hash, order.total_minor
        self.paypal_order_id = ""

    def start(self) -> str:
        started = start_paypal_checkout(
            self.s,
            self.cart_id,
            self.ref,
            self.version_hash,
            gateway=self.paypal,
            merchant_id=MERCHANT,
            clock=self.clock,
        )
        self.paypal_order_id = started.paypal_order_id
        return started.paypal_order_id

    def approved(self) -> Checkout:
        self.paypal.approve(self.start())
        return self

    def capture(self) -> payments.PaymentResult:
        return capture_payment(
            self.s,
            self.cart_id,
            self.ref,
            self.paypal_order_id,
            gateway=self.paypal,
            merchant_id=MERCHANT,
            clock=self.clock,
        )

    def reconcile(self) -> payments.PaymentResult:
        return reconcile_payment(
            self.s, self.cart_id, self.ref, gateway=self.paypal, merchant_id=MERCHANT, clock=self.clock
        )

    def command(self) -> payments.PaymentResult:
        return reconcile_by_reference(self.s, self.ref, gateway=self.paypal, merchant_id=MERCHANT, clock=self.clock)

    @property
    def status(self) -> str:
        return order_row(self.s, self.ref).status

    @property
    def hold(self) -> str:
        return order_row(self.s, self.ref).reservation_status

    def field(self, column: Any) -> Any:
        return look(self.s, lambda: self.s.execute(select(column).where(Order.public_ref == self.ref)).scalar_one())

    def evidence(self) -> list[tuple[str, str, str | None]]:
        return look(
            self.s,
            lambda: [
                (row.status, row.verification, row.problem)
                for row in self.s.execute(
                    select(Payment.status, Payment.verification, Payment.problem)
                    .join(Order, Order.id == Payment.order_id)
                    .where(Order.public_ref == self.ref)
                    .order_by(Payment.id)
                )
            ],
        )


def sales(s: Session) -> int:
    return reasons(s).count("sale")


def bag_lines(s: Session, cart_id: int) -> int:
    from app.domain.cart.models import CartItem

    return look(s, lambda: len(s.execute(select(CartItem.variant_id).where(CartItem.cart_id == cart_id)).all()))


# --- approval and the PayPal order -------------------------------------------------------------


def test_start_creates_one_paypal_order_from_the_snapshot(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 2})
    first = c.start()
    again = c.start()  # repeated (e.g. the SDK called createOrder twice)
    assert first == again
    assert len(paypal.calls_to("create_order")) == 1  # the repeat used the stored id
    assert c.status == "awaiting_payment"
    body = paypal.orders[first].body
    unit = body["purchase_units"][0]
    assert unit["invoice_id"] == c.ref
    assert unit["amount"]["value"] == "273.00"  # 2 x 129.00 + 15.00 shipping, from the snapshot
    assert paypal.calls_to("create_order")[0][2] == f"atlas-{c.ref}-create"


def test_start_refuses_a_stale_hash_and_a_hold_about_to_expire(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1})
    c.version_hash = "0" * 64
    with pytest.raises(OrderChanged):
        c.start()
    c2 = Checkout(s, clock, paypal, {"A-M": 1})
    c2.clock = FixedClock(NOW + HOLD - CAPTURE_MARGIN)
    with pytest.raises(HoldExpiring):
        c2.start()
    assert (c.status, c2.status) == ("pending_approval", "pending_approval")
    assert paypal.calls_to("create_order") == []


def test_a_create_timeout_is_retried_with_the_same_request_id(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1})
    paypal.create_failures.append(PayPalUnavailable("ReadTimeout"))
    with pytest.raises(PaymentProviderUnavailable):
        c.start()
    assert c.status == "approved"
    c.start()
    request_ids = {call[2] for call in paypal.calls_to("create_order")}
    assert request_ids == {f"atlas-{c.ref}-create"}
    assert len(paypal.orders) == 1
    assert c.status == "awaiting_payment"


# --- capture -----------------------------------------------------------------------------------


def test_a_completed_capture_commits_stock_exactly_once(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 2}).approved()
    assert stock(s, "A-S") == (5, 2)

    result = c.capture()

    assert (result.result, result.order.status, c.hold) == ("paid", "paid", "committed")
    assert stock(s, "A-S") == (3, 0)
    assert sales(s) == 1
    assert c.evidence() == [("COMPLETED", "verified", None)]
    assert bag_lines(s, c.cart_id) == 0  # the bag was bought
    for repeat in (c.capture(), c.reconcile(), c.command()):  # retries, the customer, the merchant
        assert repeat.result == "paid"
    assert paypal.captures_made == 1
    assert len(paypal.calls_to("capture_order")) == 1
    assert stock(s, "A-S") == (3, 0)
    assert sales(s) == 1
    assert len(c.evidence()) == 1
    set_quantity(s, c.cart_id, "A-M", 1, clock=clock)  # the cart is usable again


def test_capture_needs_the_stored_paypal_order_and_the_owner(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    stranger = Checkout(s, clock, paypal, {"A-M": 1})
    with pytest.raises(PayPalOrderMismatch):
        capture_payment(s, c.cart_id, c.ref, "PAYPAL-ORDER-9999", gateway=paypal, merchant_id=MERCHANT, clock=clock)
    with pytest.raises(OrderNotFound):
        capture_payment(
            s, stranger.cart_id, c.ref, c.paypal_order_id, gateway=paypal, merchant_id=MERCHANT, clock=clock
        )
    assert paypal.calls_to("capture_order") == []
    assert c.status == "awaiting_payment"


def test_a_pending_capture_keeps_the_hold_until_paypal_completes_it(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.PENDING)
    result = c.capture()
    assert (result.result, c.status, c.hold) == ("pending", "payment_pending", "order_hold")
    assert stock(s, "A-S") == (5, 1)
    clock.advance(HOLD * 2)  # well past the order's hold deadline: a pending payment keeps the stock
    assert expire_order_if_due(s, order_row(s, c.ref).id, clock=clock) is False
    with pytest.raises(PaymentInProgress):
        cancel_order(s, c.cart_id, c.ref, clock=clock)
    assert c.reconcile().result == "pending"

    paypal.settle(next(iter(paypal.captures)))
    assert c.reconcile().result == "paid"
    assert stock(s, "A-S") == (4, 0)
    assert c.evidence() == [("PENDING", "verified", None), ("COMPLETED", "verified", None)]


def test_an_ambiguous_failure_is_not_a_failure_and_is_retried_with_the_same_request_id(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.SERVER_ERROR_BEFORE)
    assert c.capture().result == "processing"
    assert (c.status, c.hold) == ("capturing", "order_hold")

    assert c.reconcile().result == "processing"  # too soon: the request might still be in flight
    assert c.capture().result == "processing"  # a retried capture reconciles; it never captures blindly
    assert len(paypal.calls_to("capture_order")) == 1

    clock.advance(STALE_CAPTURE_AFTER)
    assert c.reconcile().result == "paid"
    captures = paypal.calls_to("capture_order")
    assert len(captures) == 2
    assert captures[0][2] == captures[1][2] == f"atlas-{c.ref}-capture-1"  # the same logical request
    assert paypal.captures_made == 1


def test_a_timeout_after_paypal_captured_is_reconciled_to_paid(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.TIMEOUT_AFTER_CAPTURE)
    assert c.capture().result == "processing"
    assert stock(s, "A-S") == (5, 1)  # not committed on an unknown outcome
    assert c.reconcile().result == "paid"  # PayPal's order shows the capture
    assert len(paypal.calls_to("capture_order")) == 1  # found, not captured again
    assert paypal.captures_made == 1
    assert stock(s, "A-S") == (4, 0)


class Crash(BaseException):
    """The process dies after the claim committed and before PayPal answered."""


def test_crash_recovery_from_a_stale_capturing_order(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()

    def crash() -> None:
        raise Crash()

    paypal.before_capture = crash
    with pytest.raises(Crash):
        c.capture()
    paypal.before_capture = None
    assert c.status == "capturing"
    assert c.field(Order.capture_request_id) == f"atlas-{c.ref}-capture-1"  # persisted before the call
    assert c.field(Order.capture_attempt) == 1

    clock.advance(STALE_CAPTURE_AFTER)
    assert c.capture().result == "paid"  # reconciled first (no capture at PayPal), then the same request
    assert [call[2] for call in paypal.calls_to("capture_order")] == [f"atlas-{c.ref}-capture-1"]
    assert paypal.captures_made == 1


def test_instrument_declined_is_recoverable_and_keeps_the_hold(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.INSTRUMENT_DECLINED)
    result = c.capture()
    assert (result.result, result.recoverable) == ("instrument_declined", True)
    assert (c.status, c.hold) == ("awaiting_payment", "order_hold")
    assert stock(s, "A-S") == (5, 1)
    assert c.evidence() == []  # no capture exists

    second = c.capture()  # the buyer chose another funding source (actions.restart())
    assert second.result == "paid"
    assert [call[2] for call in paypal.calls_to("capture_order")] == [
        f"atlas-{c.ref}-capture-1",
        f"atlas-{c.ref}-capture-2",  # a new attempt only after the recoverable failure
    ]


def test_capture_before_the_buyer_approved(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1})
    c.start()  # never approved in PayPal
    result = c.capture()
    assert (result.result, result.recoverable, c.status) == ("payer_action_required", True, "awaiting_payment")


@pytest.mark.parametrize("outcome", [fake.DECLINED, fake.FAILED])
def test_a_definitive_decline_releases_the_hold(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, outcome: str
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 2}).approved()
    paypal.capture_outcomes.append(outcome)
    assert c.capture().result == "failed"
    assert (c.status, c.hold) == ("payment_failed", "released")
    assert stock(s, "A-S") == (5, 0)
    assert c.evidence() == [(fake.CAPTURE_STATUS[outcome], "verified", None)]
    assert paypal.calls_to("get_capture")  # decided from PayPal's capture record


def test_a_voided_paypal_order_releases_the_hold(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.SERVER_ERROR_BEFORE)
    c.capture()
    paypal.void(c.paypal_order_id)
    assert c.reconcile().result == "failed"
    assert c.hold == "released"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"amount_value": "128.99"}, "amount_mismatch"),
        ({"currency": "EUR"}, "currency_mismatch"),
        ({"invoice_id": "f" * 32}, "reference_mismatch"),
        ({"payee_merchant_id": "ZZZZZZZZZZZZZ"}, "payee_mismatch"),
        ({"payee_merchant_id": None}, "payee_mismatch"),  # missing merchant id: fail closed
        ({"related_order_id": "PAYPAL-ORDER-0999"}, "order_mismatch"),
    ],
)
def test_a_mismatched_capture_needs_review_and_keeps_the_hold(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal, change: dict[str, Any], problem: str
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()

    def tamper(record: CaptureRecord) -> CaptureRecord:
        return replace(record, **change)

    paypal.tamper = tamper
    assert c.capture().result == "needs_review"
    assert (c.status, c.field(Order.review_reason), c.hold) == ("needs_review", problem, "order_hold")
    assert stock(s, "A-S") == (5, 1)
    assert sales(s) == 0
    assert c.evidence() == [("COMPLETED", "mismatch", problem)]
    assert c.reconcile().result == "needs_review"  # stays with a person
    with pytest.raises(CheckoutInProgress):
        set_quantity(s, c.cart_id, "A-S", 2, clock=clock)


def test_a_payment_after_the_order_ended_needs_review_without_moving_stock(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    cancel_order(s, c.cart_id, c.ref, clock=clock)  # cancelled while the buyer was in PayPal
    with pytest.raises(OrderNotPayable):
        c.capture()
    assert paypal.calls_to("capture_order") == []  # Atlas never captures an ended order
    before = stock(s, "A-S")
    paypal.capture_order(c.paypal_order_id, request_id="outside-atlas")  # e.g. from PayPal's dashboard

    result = c.command()  # the merchant reconciles

    assert (result.result, c.status, c.field(Order.review_reason)) == (
        "needs_review",
        "needs_review",
        "capture_after_end",
    )
    assert c.evidence() == [("COMPLETED", "verified", None)]  # the payment is recorded, not discarded
    assert stock(s, "A-S") == before  # nothing released or committed automatically
    assert sales(s) == 0


def test_capture_after_end_on_a_cart_with_another_open_order_is_flagged(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    cancel_order(s, c.cart_id, c.ref, clock=clock)
    reserve_cart(s, c.cart_id, clock=clock, ttl=TTL)
    create_order(s, c.cart_id, ship_country="FR", lang="en", clock=clock, hold=HOLD)  # a new open order
    paypal.capture_order(c.paypal_order_id, request_id="outside-atlas")
    assert c.command().result == "needs_review"
    assert (c.status, c.field(Order.review_reason)) == ("cancelled", "capture_after_end")


def test_an_unresolved_payment_goes_to_review_after_the_window(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.PENDING)
    c.capture()
    clock.advance(RESOLUTION_WINDOW)
    assert c.reconcile().result == "needs_review"
    assert (c.status, c.field(Order.review_reason), c.hold) == ("needs_review", "unresolved_timeout", "order_hold")
    paypal.settle(next(iter(paypal.captures)))
    assert c.command().result == "paid"  # PayPal's evidence resolves it
    assert c.field(Order.review_reason) is None


# --- cancel and expiry around payment ----------------------------------------------------------


def test_cancel_and_expiry_never_touch_a_capturing_order(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    paypal.capture_outcomes.append(fake.SERVER_ERROR_BEFORE)
    c.capture()
    late = FixedClock(NOW + HOLD + timedelta(hours=1))
    with pytest.raises(PaymentInProgress):
        cancel_order(s, c.cart_id, c.ref, clock=late)
    assert expire_order_if_due(s, order_row(s, c.ref).id, clock=late) is False
    assert (c.status, c.hold) == ("capturing", "order_hold")


def test_capture_needs_the_margin_and_an_open_order(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    c.clock = FixedClock(NOW + HOLD - CAPTURE_MARGIN)
    with pytest.raises(HoldExpiring):
        c.capture()
    assert c.status == "awaiting_payment"
    c.clock = FixedClock(NOW + HOLD)
    assert expire_order_if_due(s, order_row(s, c.ref).id, clock=c.clock)  # an awaiting order can still expire
    with pytest.raises(OrderNotPayable):
        c.capture()
    assert paypal.calls_to("capture_order") == []


def test_no_transaction_is_open_during_paypal_calls(s: Session, clock: FixedClock, paypal: fake.FakePayPal) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1})
    seen: list[bool] = []
    real_create, real_capture, real_get = paypal.create_order, paypal.capture_order, paypal.get_capture

    def watch(call: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            seen.append(s.in_transaction())
            return call(*args, **kwargs)

        return wrapper

    paypal.create_order, paypal.capture_order, paypal.get_capture = (  # type: ignore[method-assign]
        watch(real_create),
        watch(real_capture),
        watch(real_get),
    )
    c.approved()
    c.capture()
    assert seen == [False, False, False]


def test_nothing_from_paypal_beyond_the_verification_fields_is_stored(
    s: Session, clock: FixedClock, paypal: fake.FakePayPal
) -> None:
    c = Checkout(s, clock, paypal, {"A-S": 1}).approved()
    c.capture()
    columns = {column.name for column in Payment.__table__.columns}
    assert columns == {
        "id",
        "order_id",
        "paypal_order_id",
        "capture_id",
        "status",
        "status_reason",
        "amount_minor",
        "currency",
        "invoice_id",
        "related_order_id",
        "payee_merchant_id",
        "verification",
        "problem",
        "source",
        "debug_id",
        "created_at",
    }
    row = look(s, lambda: s.execute(select(Payment)).scalar_one())
    assert (row.amount_minor, row.currency, row.invoice_id, row.payee_merchant_id) == (c.total, "USD", c.ref, MERCHANT)
    assert look(s, lambda: s.execute(select(InventoryMovement.reason)).scalars().all()).count("sale") == 1
