"""Deterministic PostgreSQL concurrency tests for payments (M3.2, ADR-026).

Same harness as test_reservation_concurrency.py: a blocker connection holds the contended row
lock, operations start in threads, the test waits until they are all blocked on it
(pg_stat_activity), then releases it. The fake PayPal counts real capture side effects, so the
tests can assert PayPal captured once and Atlas committed stock once.
"""

# The harness's fixtures (engine, factory, race, clock) are imported so pytest finds them here;
# test parameters with the same names are how fixtures are requested, not redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FixedClock
from app.domain.orders.holds import expire_order_if_due
from app.domain.orders.models import Order
from app.domain.orders.service import PaymentInProgress, cancel_order, create_order
from app.domain.payments.models import Payment, PayPalWebhookEvent
from app.domain.payments.paypal import PayPalUnavailable
from app.domain.payments.service import (
    CAPTURE_MARGIN,
    OrderNotPayable,
    capture_payment,
    finalize,
    start_paypal_checkout,
)
from app.domain.payments.webhooks import COMPLETED, WebhookRetryLater, handle_webhook
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import reserve_cart
from tests import fake_paypal as fake
from tests.conftest import TEST_DATABASE_URL
from tests.factories import V, make_product
from tests.integration.test_paypal_webhooks import TRANSMISSION
from tests.integration.test_reservation_concurrency import (  # noqa: F401  (fixtures: engine, factory, race, clock)
    RESULT_TIMEOUT,
    Race,
    blocker,
    check_invariants,
    clock,
    engine,
    factory,
    movement_reasons,
    new_cart,
    race,
    setup,
    stock,
    wait_for_lock_waiters,
)
from tests.order_support import HOLD, add_shipping
from tests.reservation_builders import NOW, TTL

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="real concurrency needs PostgreSQL (TEST_DATABASE_URL)"),
]

MERCHANT = fake.MERCHANT_ID


def shop(session: Session) -> None:
    make_product(session, "p", variants=[V(sku="P-M", on_hand=5)])
    add_shipping(session)


def approved_order(factory: sessionmaker[Session], clock: FixedClock, paypal: fake.FakePayPal) -> tuple[int, str, str]:
    """A cart whose order is awaiting payment, approved by the buyer at PayPal."""
    cart_id = new_cart(factory, clock, {"P-M": 2})
    with factory() as session:
        reserve_cart(session, cart_id, clock=clock, ttl=TTL)
        order = create_order(session, cart_id, ship_country="FR", lang="en", clock=clock, hold=HOLD).order
        started = start_paypal_checkout(
            session,
            cart_id,
            order.public_ref,
            order.version_hash,
            gateway=paypal,
            merchant_id=MERCHANT,
            site_origin="http://testserver",
            clock=clock,
        )
    paypal.approve(started.paypal_order_id)
    return cart_id, order.public_ref, started.paypal_order_id


def order_state(factory: sessionmaker[Session]) -> tuple[str, str]:
    with factory() as session:
        row = session.execute(
            select(Order.status, Reservation.status).join(Reservation, Reservation.id == Order.reservation_id)
        ).one()
    return row[0], row[1]


def order_id(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.execute(select(Order.id)).scalar_one()


def payments(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return len(session.execute(select(Payment.id)).all())


def capture(cart_id: int, ref: str, paypal_order_id: str, paypal: fake.FakePayPal, clock: FixedClock) -> Any:
    return lambda s: capture_payment(
        s, cart_id, ref, paypal_order_id, gateway=paypal, merchant_id=MERCHANT, clock=clock
    )


# --- C1: parallel capture requests -------------------------------------------------------------


def test_c1_parallel_captures_capture_once_and_commit_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    cart_id, ref, paypal_order_id = approved_order(factory, clock, paypal)

    hold = blocker(engine, "SELECT id FROM orders WHERE id = :id FOR NO KEY UPDATE", id=order_id(factory))
    for _ in range(4):
        race.start(capture(cart_id, ref, paypal_order_id, paypal, clock))
    wait_for_lock_waiters(engine, 4, race.futures)  # all four at the capture claim
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    assert [o for o in outcomes if isinstance(o, BaseException)] == []
    assert {o.result for o in outcomes} <= {"paid", "processing"}
    assert paypal.captures_made == 1
    assert len(paypal.calls_to("capture_order")) == 1  # the losers asked PayPal, never captured
    assert order_state(factory) == ("paid", "committed")
    assert movement_reasons(factory).count("sale") == 1
    assert stock(factory, "P-M") == (3, 0)
    check_invariants(factory)


# --- C2: two finalizations of the same capture (capture response vs reconcile / webhook) -----------


def test_c2_concurrent_finalizations_commit_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    paypal.capture_outcomes.append(fake.TIMEOUT_AFTER_CAPTURE)
    cart_id, ref, paypal_order_id = approved_order(factory, clock, paypal)
    with factory() as session:
        assert capture(cart_id, ref, paypal_order_id, paypal, clock)(session).result == "processing"
    record = paypal.get_capture(next(iter(paypal.captures)))
    oid = order_id(factory)

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    for source in ("capture", "reconcile", "reconcile"):
        race.start(lambda s, src=source: finalize(s, oid, record, merchant_id=MERCHANT, clock=clock, source=src))
    wait_for_lock_waiters(engine, 3, race.futures)  # all three at the cart lock
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    assert outcomes == ["paid", "paid", "paid"]
    assert order_state(factory) == ("paid", "committed")
    assert movement_reasons(factory).count("sale") == 1
    assert payments(factory) == 1  # the evidence is recorded once
    check_invariants(factory)


# --- C3: cancel racing the capture claim ---------------------------------------------------------


def test_c3_cancel_and_capture_claim_exactly_one_wins(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    cart_id, ref, paypal_order_id = approved_order(factory, clock, paypal)

    hold = blocker(engine, "SELECT id FROM orders WHERE id = :id FOR NO KEY UPDATE", id=order_id(factory))
    captured = race.start(capture(cart_id, ref, paypal_order_id, paypal, clock))
    cancelled = race.start(lambda s: cancel_order(s, cart_id, ref, clock=clock))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    race.outcomes()
    cancel_error = cancelled.exception(timeout=RESULT_TIMEOUT)
    if cancel_error is None:  # the cancel won: Atlas never captured
        assert paypal.captures_made == 0
        assert order_state(factory) == ("cancelled", "released")
        assert captured.result().result == "closed" or isinstance(captured.exception(), OrderNotPayable)
    else:  # the capture claim won: the hold is protected
        assert isinstance(cancel_error, PaymentInProgress)
        assert captured.result().result == "paid"
        assert order_state(factory) == ("paid", "committed")
    check_invariants(factory)


# --- C4: lazy expiry racing the capture claim at the deadline --------------------------------------


def test_c4_expiry_and_capture_claim_never_both_apply(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    cart_id, ref, paypal_order_id = approved_order(factory, clock, paypal)
    oid = order_id(factory)
    just_in_time = FixedClock(NOW + HOLD - CAPTURE_MARGIN - timedelta(seconds=1))  # the claim's last moment
    late = FixedClock(NOW + HOLD)  # due for expiry

    hold = blocker(engine, "SELECT id FROM orders WHERE id = :id FOR NO KEY UPDATE", id=oid)
    captured = race.start(capture(cart_id, ref, paypal_order_id, paypal, just_in_time))
    expired = race.start(lambda s: expire_order_if_due(s, oid, clock=late))
    wait_for_lock_waiters(engine, 2, race.futures)
    hold.rollback()
    hold.close()

    race.outcomes()
    if expired.result(timeout=RESULT_TIMEOUT):  # expiry won: no capture was ever sent
        assert paypal.captures_made == 0
        assert order_state(factory) == ("expired", "expired")
        assert captured.result().result == "closed"
    else:  # the claim won: expiry no longer applies to a capturing order
        assert captured.result().result == "paid"
        assert order_state(factory) == ("paid", "committed")
    check_invariants(factory)


# --- W: PayPal webhooks (M3.3) ----------------------------------------------------------------------


def completed_event(ref: str, paypal_order_id: str, event_id: str = "WH-RACE") -> bytes:
    resource = {
        "id": "CAPTURE-CLAIMED",
        "status": "COMPLETED",
        "invoice_id": ref,
        "supplementary_data": {"related_ids": {"order_id": paypal_order_id}},
    }
    return json.dumps({"id": event_id, "event_type": COMPLETED, "resource": resource}).encode("utf-8")


def webhook(raw: bytes, paypal: fake.FakePayPal, clock: FixedClock) -> Any:
    return lambda s: handle_webhook(
        s, raw, TRANSMISSION, gateway=paypal, webhook_id=fake.WEBHOOK_ID, merchant_id=MERCHANT, clock=clock
    )


def event_row(factory: sessionmaker[Session]) -> tuple[int, str | None, bool]:
    with factory() as session:
        row = session.execute(select(PayPalWebhookEvent)).scalar_one()
        return row.deliveries, row.outcome, row.processed_at is not None


def lost_capture(factory: sessionmaker[Session], clock: FixedClock, paypal: fake.FakePayPal) -> tuple[int, str, str]:
    cart_id, ref, paypal_order_id = approved_order(factory, clock, paypal)
    paypal.capture_outcomes.append(fake.TIMEOUT_AFTER_CAPTURE)
    with factory() as session:
        assert capture(cart_id, ref, paypal_order_id, paypal, clock)(session).result == "processing"
    return cart_id, ref, paypal_order_id


def test_w1_concurrent_deliveries_of_one_event_commit_once_and_count_every_delivery(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    cart_id, ref, paypal_order_id = lost_capture(factory, clock, paypal)
    raw = completed_event(ref, paypal_order_id)

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    for _ in range(4):
        race.start(webhook(raw, paypal, clock))
    wait_for_lock_waiters(engine, 4, race.futures)  # all four recorded, reconciled, and at finalize
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    assert [o for o in outcomes if isinstance(o, BaseException)] == []
    assert {o.outcome for o in outcomes} == {"paid"}
    assert order_state(factory) == ("paid", "committed")
    assert movement_reasons(factory).count("sale") == 1
    assert payments(factory) == 1
    assert event_row(factory) == (4, "paid", True)
    assert paypal.captures_made == 1
    assert len(paypal.calls_to("capture_order")) == 1  # no webhook captured
    check_invariants(factory)


def test_w2_concurrent_redeliveries_never_lose_a_delivery_count(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    _cart_id, ref, paypal_order_id = lost_capture(factory, clock, paypal)
    raw = completed_event(ref, paypal_order_id)
    paypal.get_failures.append(PayPalUnavailable("ReadTimeout"))
    with factory() as session, pytest.raises(WebhookRetryLater):
        webhook(raw, paypal, clock)(session)  # recorded, not processed
    assert event_row(factory) == (1, None, False)

    hold = blocker(engine, "SELECT id FROM paypal_webhook_events FOR UPDATE")
    for _ in range(5):
        race.start(webhook(raw, paypal, clock))
    wait_for_lock_waiters(engine, 5, race.futures)  # all five at the delivery upsert
    hold.rollback()
    hold.close()

    outcomes = race.outcomes()
    assert [o for o in outcomes if isinstance(o, BaseException)] == []
    assert {o.outcome for o in outcomes} == {"paid"}
    assert event_row(factory) == (6, "paid", True)  # 1 + 5: no increment lost
    assert order_state(factory) == ("paid", "committed")
    assert movement_reasons(factory).count("sale") == 1
    check_invariants(factory)


def test_w3_a_webhook_racing_the_capture_response_commits_once(
    engine: Engine, factory: sessionmaker[Session], race: Race, clock: FixedClock
) -> None:
    setup(factory, shop)
    paypal = fake.FakePayPal()
    cart_id, ref, paypal_order_id = approved_order(factory, clock, paypal)

    hold = blocker(engine, "SELECT id FROM carts WHERE id = :id FOR NO KEY UPDATE", id=cart_id)
    captured = race.start(capture(cart_id, ref, paypal_order_id, paypal, clock))
    wait_for_lock_waiters(engine, 1, race.futures)  # captured at PayPal, waiting to finalize
    assert paypal.captures_made == 1
    notified = race.start(webhook(completed_event(ref, paypal_order_id), paypal, clock))
    wait_for_lock_waiters(engine, 2, race.futures)  # the webhook reached finalize too
    hold.rollback()
    hold.close()

    race.outcomes()
    assert captured.result().result == "paid"
    assert notified.result().outcome == "paid"
    assert order_state(factory) == ("paid", "committed")
    assert movement_reasons(factory).count("sale") == 1
    assert payments(factory) == 1
    assert event_row(factory) == (1, "paid", True)
    check_invariants(factory)
