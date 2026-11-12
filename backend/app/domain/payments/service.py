"""PayPal Sandbox checkout: approve, create, capture, reconcile and finalize (M3.2, ADR-026).

**No transaction or row lock is ever held during a PayPal call.** Every step is: a short
transaction that checks and claims the step and stores the identifiers it will use, then the
PayPal call, then a short transaction that applies the result. The browser never decides
anything: it identifies the order (cookie + reference) and forwards PayPal's order id, which must
equal the stored one. Payment is decided only from PayPal's own capture record.

Flow and state transitions (compare-and-set on ``orders.status`` each time):

1. ``start_paypal_checkout``: ``pending_approval`` -> ``approved`` (the presented version hash
   matches; the hold has more than ``CAPTURE_MARGIN`` left), storing the stable create request
   id; then PayPal create; then ``approved`` -> ``awaiting_payment`` storing the PayPal order id
   (written once). A retry reuses the same request id, so PayPal returns the same order.
2. ``capture_payment``: ``awaiting_payment`` -> ``capturing``, persisting the attempt number,
   request id and time **before** calling PayPal (crash recovery). Then capture, then:
   - a capture in the response: read PayPal's capture record and **finalize**;
   - ``INSTRUMENT_DECLINED`` (recoverable funding failure, no capture created): back to
     ``awaiting_payment``, hold kept, result ``instrument_declined`` (the buyer picks another
     funding source with ``actions.restart()``; the next capture is a new attempt);
   - ``ORDER_NOT_APPROVED`` / ``PAYER_ACTION_REQUIRED``: no capture can exist, back to
     ``awaiting_payment``;
   - timeout, connection failure, 5xx, any other rejection: the outcome is unknown. The order
     stays ``capturing`` with the hold, and is reconciled; never treated as a failure.
3. ``reconcile_payment`` (also what a retried capture does when the order is ``capturing`` or
   ``payment_pending``): read the PayPal order. A capture there is finalized. No capture and the
   PayPal order still ``APPROVED``: if the claimed attempt is stale (``STALE_CAPTURE_AFTER``,
   longer than any live request), the capture is retried with the **same** request id, so PayPal
   cannot capture twice; otherwise it is still in flight. ``VOIDED``: no payment can exist, the
   hold is released (``payment_failed``).
4. ``finalize`` is the single place every outcome converges (capture response, reconcile, and
   M3.3's webhooks). One transaction, lock order cart -> order -> reservation -> variants:
   record the evidence, then
   - ``COMPLETED`` and verified, order still holding stock: ``paid``, reservation ``committed``,
     ``sale`` movements, bag emptied, exactly once;
   - ``COMPLETED`` but the order already ended (cancelled / expired / failed): evidence kept,
     ``needs_review`` (``capture_after_end``), stock untouched, never fulfilled automatically;
   - verification fails (amount, USD, reference, PayPal order, merchant): ``needs_review``,
     hold kept;
   - ``PENDING``: ``payment_pending``, hold kept;
   - ``DECLINED`` / ``FAILED`` (definitive, from PayPal's capture record): ``payment_failed``,
     hold released;
   - anything else: ``needs_review`` (``unexpected_status``), hold kept.
5. Still ``capturing`` or ``payment_pending`` ``RESOLUTION_WINDOW`` after the attempt started:
   ``needs_review`` (``unresolved_timeout``), hold kept. A later verified completion can still
   finalize it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from sqlalchemy import delete, exists, insert, select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import mark_rollback_only, unit_of_work
from app.domain.cart.models import Cart, CartItem
from app.domain.inventory.service import (
    InventoryInvariantViolation,
    commit_reservation_stock,
    release_reservation_stock,
)
from app.domain.orders.holds import open_order_ref
from app.domain.orders.models import FINAL_STATUSES, OPEN_STATUSES, Order
from app.domain.orders.service import OrderView, _order_view, _owned_order_id
from app.domain.payments.models import Payment
from app.domain.payments.paypal import (
    CaptureRecord,
    PayPalGateway,
    PayPalOrder,
    PayPalRejected,
    PayPalUnavailable,
    from_paypal_value,
)
from app.domain.payments.verify import Expected, capture_problem, order_request
from app.domain.reservations.models import Reservation
from app.errors import AtlasError

logger = logging.getLogger(__name__)

CAPTURE_MARGIN = timedelta(minutes=2)  # approved decision: a capture starts only with this much hold left
RESOLUTION_WINDOW = timedelta(hours=72)  # approved decision: then needs_review, hold kept
STALE_CAPTURE_AFTER = timedelta(seconds=60)  # longer than the client's 25 s worst case per request

# Results returned to the client (the order's stored status is the truth; this says what happened).
PAID = "paid"
PENDING = "pending"
PROCESSING = "processing"  # outcome not known yet: try reconciling again shortly
INSTRUMENT_DECLINED = "instrument_declined"  # recoverable: actions.restart() and pay with something else
PAYER_ACTION_REQUIRED = "payer_action_required"  # recoverable: the buyer must (re)approve in PayPal
FAILED = "failed"
NEEDS_REVIEW = "needs_review"
AWAITING_PAYMENT = "awaiting_payment"
CLOSED = "closed"  # cancelled or expired before payment
RECOVERABLE = frozenset({INSTRUMENT_DECLINED, PAYER_ACTION_REQUIRED})

_BY_STATUS = {
    "paid": PAID,
    "payment_pending": PENDING,
    "capturing": PROCESSING,
    "payment_failed": FAILED,
    "needs_review": NEEDS_REVIEW,
    "awaiting_payment": AWAITING_PAYMENT,
    "cancelled": CLOSED,
    "expired": CLOSED,
}


# --- Results and errors ------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutStart:
    paypal_order_id: str
    order: OrderView


@dataclass(frozen=True)
class PaymentResult:
    result: str
    order: OrderView

    @property
    def recoverable(self) -> bool:
        return self.result in RECOVERABLE


class PaymentsUnavailable(AtlasError):
    status_code = 503
    code = "payments_unavailable"

    def __init__(self) -> None:
        super().__init__("Payments are not available right now.")


class PaymentProviderUnavailable(AtlasError):
    status_code = 503
    code = "payment_provider_unavailable"

    def __init__(self) -> None:
        super().__init__("PayPal could not be reached. Please try again; nothing was charged twice.")


class PaymentProviderRejected(AtlasError):
    status_code = 502
    code = "payment_provider_rejected"

    def __init__(self, issue: str | None) -> None:
        super().__init__("PayPal could not start this payment.", details=[{"issue": issue}] if issue else [])


class OrderChanged(AtlasError):
    status_code = 409
    code = "order_changed"

    def __init__(self) -> None:
        super().__init__("This order is not the one you reviewed. Please review it again.")


class HoldExpiring(AtlasError):
    status_code = 409
    code = "hold_expiring"

    def __init__(self) -> None:
        super().__init__("This order's reservation is about to expire, so payment can't start. Please review it again.")


class OrderNotPayable(AtlasError):
    status_code = 409
    code = "order_not_payable"

    def __init__(self, status: str) -> None:
        super().__init__("This order can't be paid now.", details=[{"status": status}])


class PayPalOrderMismatch(AtlasError):
    status_code = 409
    code = "paypal_order_mismatch"

    def __init__(self) -> None:
        super().__init__("This PayPal order does not belong to this order.")


def _fail(session: Session, error: AtlasError) -> NoReturn:
    mark_rollback_only(session, error.code)
    raise error


# --- Reads -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _State:
    id: int
    public_ref: str
    cart_id: int
    reservation_id: int
    status: str
    version_hash: str
    total_minor: int
    hold_expires_at: datetime
    paypal_order_id: str | None
    create_request_id: str | None
    capture_attempt: int
    capture_request_id: str | None
    capture_started_at: datetime | None
    review_reason: str | None


def _state(session: Session, order_id: int) -> _State:
    row = session.execute(
        select(
            Order.id,
            Order.public_ref,
            Order.cart_id,
            Order.reservation_id,
            Order.status,
            Order.version_hash,
            Order.total_minor,
            Order.hold_expires_at,
            Order.paypal_order_id,
            Order.paypal_create_request_id.label("create_request_id"),
            Order.capture_attempt,
            Order.capture_request_id,
            Order.capture_started_at,
            Order.review_reason,
        ).where(Order.id == order_id)
    ).one()
    return _State(**row._mapping)


def _set(session: Session, order_id: int, from_status: str | tuple[str, ...], now: datetime, **values: object) -> bool:
    """Compare-and-set on the order's status. Returns whether this call made the change."""
    statuses = (from_status,) if isinstance(from_status, str) else from_status
    changed = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(statuses))
        .values(updated_at=now, **values)
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    return changed is not None


def _result(session: Session, order_id: int, result: str | None = None) -> PaymentResult:
    with unit_of_work(session):
        view = _order_view(session, order_id)
    return PaymentResult(result or _BY_STATUS.get(view.status, CLOSED), view)


# --- 1. Approve and create the PayPal order ------------------------------------------------------


def start_paypal_checkout(
    session: Session,
    cart_id: int,
    public_ref: str,
    version_hash: str,
    *,
    gateway: PayPalGateway,
    merchant_id: str,
    site_origin: str,
    clock: Clock,
) -> CheckoutStart:
    """Approve the reviewed snapshot and create its one PayPal order. Safe to repeat."""
    with unit_of_work(session):
        now = clock.now()
        order_id = _owned_order_id(session, cart_id, public_ref)
        session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == "pending_approval",
                Order.version_hash == version_hash,
                Order.hold_expires_at > now + CAPTURE_MARGIN,
            )
            .values(
                status="approved",
                approved_at=now,
                paypal_create_request_id=f"atlas-{public_ref}-create",
                updated_at=now,
                status_changed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        state = _state(session, order_id)
        if state.version_hash != version_hash:
            raise OrderChanged()
        if state.status == "awaiting_payment" and state.paypal_order_id:
            return CheckoutStart(state.paypal_order_id, _order_view(session, order_id))  # a repeat
        if state.status == "pending_approval" or (
            state.status == "approved" and state.hold_expires_at <= now + CAPTURE_MARGIN
        ):
            raise HoldExpiring()
        if state.status != "approved" or state.create_request_id is None:
            raise OrderNotPayable(state.status)
        view = _order_view(session, order_id)
        request_id = state.create_request_id

    try:
        created = gateway.create_order(
            order_request(view, merchant_id=merchant_id, site_origin=site_origin), request_id=request_id
        )
    except PayPalUnavailable as error:
        logger.warning("paypal create unavailable", extra={"order_ref": public_ref, "paypal_debug_id": error.debug_id})
        raise PaymentProviderUnavailable() from None
    except PayPalRejected as error:
        logger.warning(
            "paypal create rejected",
            extra={"order_ref": public_ref, "paypal_issue": error.issue, "paypal_debug_id": error.debug_id},
        )
        raise PaymentProviderRejected(error.issue) from None

    with unit_of_work(session):
        now = clock.now()
        recorded = session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == "approved", Order.paypal_order_id.is_(None))
            .values(status="awaiting_payment", paypal_order_id=created.id, updated_at=now, status_changed_at=now)
            .returning(Order.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        state = _state(session, order_id)
        if recorded is None and state.paypal_order_id != created.id:
            if state.paypal_order_id is not None and state.status in OPEN_STATUSES:
                # Two PayPal orders for one Atlas order: never expected (same request id). Flag it.
                _set(
                    session,
                    order_id,
                    state.status,
                    now,
                    status="needs_review",
                    review_reason="paypal_order_conflict",
                    status_changed_at=now,
                )
            raise OrderNotPayable(state.status)
        return CheckoutStart(created.id, _order_view(session, order_id))


# --- 2. Capture ----------------------------------------------------------------------------------


def capture_payment(
    session: Session,
    cart_id: int,
    public_ref: str,
    paypal_order_id: str,
    *,
    gateway: PayPalGateway,
    merchant_id: str,
    clock: Clock,
) -> PaymentResult:
    """Capture the approved PayPal order once. Repeats and concurrent calls never capture twice."""
    with unit_of_work(session):
        now = clock.now()
        order_id = _owned_order_id(session, cart_id, public_ref)
        state = _state(session, order_id)
        if state.paypal_order_id is None or paypal_order_id != state.paypal_order_id:
            raise PayPalOrderMismatch()
        claimed: str | None = None
        if state.status == "awaiting_payment":
            if state.hold_expires_at <= now + CAPTURE_MARGIN:
                raise HoldExpiring()
            attempt = state.capture_attempt + 1
            request_id = f"atlas-{public_ref}-capture-{attempt}"
            won = session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == "awaiting_payment",
                    Order.capture_attempt == state.capture_attempt,
                    Order.hold_expires_at > now + CAPTURE_MARGIN,
                )
                .values(
                    status="capturing",
                    capture_attempt=attempt,
                    capture_request_id=request_id,
                    capture_started_at=now,
                    updated_at=now,
                    status_changed_at=now,
                )
                .returning(Order.id)
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            claimed = request_id if won is not None else None
        elif state.status in ("pending_approval", "approved", "cancelled", "expired"):
            raise OrderNotPayable(state.status)

    if claimed is not None:
        return _run_capture(session, order_id, paypal_order_id, claimed, gateway, merchant_id, clock)
    # Already capturing / pending / decided, or another request won the claim: never capture
    # blindly; find out from PayPal.
    return _reconcile(session, order_id, gateway, merchant_id, clock, allow_retry=True)


def _run_capture(
    session: Session,
    order_id: int,
    paypal_order_id: str,
    request_id: str,
    gateway: PayPalGateway,
    merchant_id: str,
    clock: Clock,
) -> PaymentResult:
    try:
        captured = gateway.capture_order(paypal_order_id, request_id=request_id)
    except PayPalUnavailable as error:
        logger.warning(
            "paypal capture outcome unknown", extra={"order_id": order_id, "paypal_debug_id": error.debug_id}
        )
        return _after(session, order_id, clock, PROCESSING)
    except PayPalRejected as error:
        logger.info(
            "paypal capture rejected",
            extra={"order_id": order_id, "paypal_issue": error.issue, "paypal_debug_id": error.debug_id},
        )
        if error.issue == "INSTRUMENT_DECLINED":
            _release_claim(session, order_id, request_id, clock)
            return _result(session, order_id, INSTRUMENT_DECLINED)
        if error.issue in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
            _release_claim(session, order_id, request_id, clock)
            return _result(session, order_id, PAYER_ACTION_REQUIRED)
        # ORDER_ALREADY_CAPTURED, *_IN_PROGRESS or anything unrecognised: ambiguous. Ask PayPal.
        return _reconcile(session, order_id, gateway, merchant_id, clock, allow_retry=False)
    result = _apply(session, order_id, captured, gateway, merchant_id, clock, source="capture")
    if result is None:  # a capture response without a capture: ask PayPal what happened
        return _reconcile(session, order_id, gateway, merchant_id, clock, allow_retry=False)
    return _after(session, order_id, clock, result)


def _release_claim(session: Session, order_id: int, request_id: str, clock: Clock) -> None:
    """PayPal proved no capture was created for this attempt: back to awaiting payment, hold kept."""
    with unit_of_work(session):
        now = clock.now()
        session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == "capturing", Order.capture_request_id == request_id)
            .values(status="awaiting_payment", updated_at=now, status_changed_at=now)
            .execution_options(synchronize_session=False)
        )


# --- 3. Reconcile ----------------------------------------------------------------------------------


def reconcile_payment(
    session: Session, cart_id: int, public_ref: str, *, gateway: PayPalGateway, merchant_id: str, clock: Clock
) -> PaymentResult:
    """The customer's explicit "check my payment" (POST): ask PayPal, apply what it says."""
    with unit_of_work(session):
        order_id = _owned_order_id(session, cart_id, public_ref)
    return _reconcile(session, order_id, gateway, merchant_id, clock, allow_retry=True)


def reconcile_by_reference(
    session: Session, public_ref: str, *, gateway: PayPalGateway, merchant_id: str, clock: Clock
) -> PaymentResult:
    """The merchant command (``python -m app.payments reconcile REF``): any order with a PayPal
    order, in any status, so a payment that turned up after the order ended is found too."""
    with unit_of_work(session):
        order_id = session.execute(select(Order.id).where(Order.public_ref == public_ref)).scalar_one_or_none()
    if order_id is None:
        raise LookupError("no such order")
    return _reconcile(session, order_id, gateway, merchant_id, clock, allow_retry=True, any_status=True)


def _reconcile(
    session: Session,
    order_id: int,
    gateway: PayPalGateway,
    merchant_id: str,
    clock: Clock,
    *,
    allow_retry: bool,
    any_status: bool = False,
) -> PaymentResult:
    with unit_of_work(session):
        state = _state(session, order_id)
    if state.paypal_order_id is None:
        return _result(session, order_id)
    if not any_status and state.status not in ("capturing", "payment_pending"):
        return _result(session, order_id)

    try:
        paypal_order = gateway.get_order(state.paypal_order_id)
    except (PayPalUnavailable, PayPalRejected) as error:
        logger.warning("paypal reconcile unavailable", extra={"order_id": order_id, "paypal_debug_id": error.debug_id})
        return _after(session, order_id, clock, PROCESSING)
    with unit_of_work(session):
        now = clock.now()
        session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(last_reconciled_at=now)
            .execution_options(synchronize_session=False)
        )

    result = _apply(session, order_id, paypal_order, gateway, merchant_id, clock, source="reconcile")
    if result is not None:
        return _after(session, order_id, clock, result)

    # No capture exists at PayPal.
    if paypal_order.status == "VOIDED":
        return _after(session, order_id, clock, _definitive_no_payment(session, order_id, clock))
    if state.status == "capturing" and state.capture_request_id is not None:
        if paypal_order.status == "APPROVED":
            started = state.capture_started_at or clock.now()
            if allow_retry and clock.now() - started >= STALE_CAPTURE_AFTER:
                # The claimed attempt is stale (e.g. the process died mid-request): retry the SAME
                # logical request with the SAME PayPal-Request-Id, so PayPal can't capture twice.
                return _run_capture(
                    session, order_id, state.paypal_order_id, state.capture_request_id, gateway, merchant_id, clock
                )
            return _after(session, order_id, clock, PROCESSING)
        if paypal_order.status in ("CREATED", "SAVED", "PAYER_ACTION_REQUIRED"):
            _release_claim(session, order_id, state.capture_request_id, clock)  # not approved: no capture possible
            return _result(session, order_id, PAYER_ACTION_REQUIRED)
    return _after(session, order_id, clock, None)


def _apply(
    session: Session,
    order_id: int,
    paypal_order: PayPalOrder,
    gateway: PayPalGateway,
    merchant_id: str,
    clock: Clock,
    *,
    source: str,
) -> str | None:
    """Finalize from PayPal's capture record(s) for this PayPal order; None if there is no capture."""
    if not paypal_order.captures:
        return None
    try:
        records = [gateway.get_capture(capture.id) for capture in paypal_order.captures]
    except (PayPalUnavailable, PayPalRejected) as error:
        logger.warning(
            "paypal capture record unavailable", extra={"order_id": order_id, "paypal_debug_id": error.debug_id}
        )
        return PROCESSING
    rank = {"COMPLETED": 0, "PENDING": 1}
    best = min(records, key=lambda record: rank.get(record.status, 2))  # one success outranks any decline
    return finalize(session, order_id, best, merchant_id=merchant_id, clock=clock, source=source)


def _after(session: Session, order_id: int, clock: Clock, result: str | None) -> PaymentResult:
    """Apply the resolution window, then report the order."""
    with unit_of_work(session):
        now = clock.now()
        moved = session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_(("capturing", "payment_pending")),
                Order.capture_started_at <= now - RESOLUTION_WINDOW,
            )
            .values(status="needs_review", review_reason="unresolved_timeout", updated_at=now, status_changed_at=now)
            .returning(Order.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
    if moved is not None:
        logger.warning("payment unresolved after the resolution window", extra={"order_id": order_id})
        return _result(session, order_id, NEEDS_REVIEW)
    return _result(session, order_id, result)


def _definitive_no_payment(session: Session, order_id: int, clock: Clock) -> str:
    """PayPal shows the order can never be paid (VOIDED, no capture): release the hold."""
    with unit_of_work(session):
        now = clock.now()
        state = _state(session, order_id)
        if state.status in ("capturing", "payment_pending") and _set(
            session, order_id, state.status, now, status="payment_failed", status_changed_at=now
        ):
            _release_hold(session, state.reservation_id, now)
            return FAILED
    return _BY_STATUS.get(state.status, CLOSED)


def _release_hold(session: Session, reservation_id: int, now: datetime) -> None:
    released = session.execute(
        update(Reservation)
        .where(Reservation.id == reservation_id, Reservation.status == "order_hold")
        .values(status="released", ended_at=now)
        .returning(Reservation.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if released is None:
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} is not an order hold."))
    release_reservation_stock(session, released, now=now)


# --- 4. Finalize (the one convergence point) ----------------------------------------------------------


def finalize(
    session: Session, order_id: int, record: CaptureRecord, *, merchant_id: str, clock: Clock, source: str
) -> str:
    """Apply PayPal's capture record to the order, idempotently, in one transaction.

    Lock order cart -> order -> reservation -> variants: the cart is locked first (its version
    advanced) because a paid order empties the bag, and that also serialises concurrent
    finalizations of the same order, so each outcome is applied, and stock committed, once.
    """
    with unit_of_work(session):
        now = clock.now()
        cart_id = session.execute(select(Order.cart_id).where(Order.id == order_id)).scalar_one()
        session.execute(
            update(Cart)
            .where(Cart.id == cart_id)
            .values(version=Cart.version + 1)
            .execution_options(synchronize_session=False)
        )
        state = _state(session, order_id)  # fresh, after the lock
        if state.paypal_order_id is None:
            _fail(session, InventoryInvariantViolation(f"Order {order_id} has no PayPal order."))
        problem = capture_problem(
            record,
            Expected(
                paypal_order_id=state.paypal_order_id,
                invoice_id=state.public_ref,
                merchant_id=merchant_id,
                total_minor=state.total_minor,
            ),
        )
        _record_evidence(session, state, record, problem, source, now)
        outcome = _decide(session, state, record, problem, now)
    log = logger.warning if outcome == NEEDS_REVIEW else logger.info
    log(
        "payment finalized",
        extra={
            "order_ref": state.public_ref,
            "capture_status": record.status,
            "outcome": outcome,
            "problem": problem,
            "paypal_debug_id": record.debug_id,
        },
    )
    return outcome


def _record_evidence(
    session: Session, state: _State, record: CaptureRecord, problem: str | None, source: str, now: datetime
) -> None:
    seen = session.execute(
        select(exists().where(Payment.capture_id == record.id, Payment.status == record.status))
    ).scalar_one()
    if seen:
        return
    session.execute(
        insert(Payment).values(
            order_id=state.id,
            paypal_order_id=state.paypal_order_id,
            capture_id=record.id,
            status=record.status[:24],
            status_reason=(record.status_reason or None) and record.status_reason[:64],
            amount_minor=from_paypal_value(record.amount_value),
            currency=(record.currency or None) and record.currency[:3],
            invoice_id=(record.invoice_id or None) and record.invoice_id[:127],
            related_order_id=(record.related_order_id or None) and record.related_order_id[:36],
            payee_merchant_id=(record.payee_merchant_id or None) and record.payee_merchant_id[:32],
            verification="verified" if problem is None else "mismatch",
            problem=problem,
            source=source,
            debug_id=(record.debug_id or None) and record.debug_id[:64],
            created_at=now,
        )
    )


def _review(session: Session, state: _State, reason: str, now: datetime) -> str:
    if state.status in OPEN_STATUSES and state.status != "needs_review":
        _set(session, state.id, state.status, now, status="needs_review", review_reason=reason, status_changed_at=now)
    return NEEDS_REVIEW


def _decide(session: Session, state: _State, record: CaptureRecord, problem: str | None, now: datetime) -> str:
    holding = state.status in OPEN_STATUSES  # the reservation is still the order's hold
    ended = state.status in FINAL_STATUSES and state.status != "paid"

    if record.status == "COMPLETED" and problem is None:
        if state.status == "paid":
            return PAID  # already finalized: nothing again
        if ended:
            return _capture_after_end(session, state, now)
        if state.status == "needs_review" and state.review_reason != "unresolved_timeout":
            return NEEDS_REVIEW  # a mismatch stays with a person
        if holding and _set(
            session, state.id, state.status, now, status="paid", review_reason=None, status_changed_at=now
        ):
            committed = session.execute(
                update(Reservation)
                .where(Reservation.id == state.reservation_id, Reservation.status == "order_hold")
                .values(status="committed", ended_at=now)
                .returning(Reservation.id)
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            if committed is None:
                _fail(session, InventoryInvariantViolation(f"Order {state.id} does not hold its reservation."))
            commit_reservation_stock(session, committed, now=now)
            session.execute(delete(CartItem).where(CartItem.cart_id == state.cart_id))  # the bag was bought
            return PAID
        return _BY_STATUS.get(state.status, CLOSED)

    if problem is not None:
        if record.status == "COMPLETED" and ended:
            return _capture_after_end(session, state, now)
        return _review(session, state, problem, now) if holding else _BY_STATUS.get(state.status, CLOSED)

    if record.status == "PENDING":
        if state.status == "capturing":
            _set(session, state.id, "capturing", now, status="payment_pending", status_changed_at=now)
        if state.status in ("capturing", "payment_pending"):
            return PENDING
        return _BY_STATUS.get(state.status, CLOSED)

    if record.status in ("DECLINED", "FAILED"):
        if state.status in ("capturing", "payment_pending") and _set(
            session, state.id, state.status, now, status="payment_failed", status_changed_at=now
        ):
            _release_hold(session, state.reservation_id, now)
            return FAILED
        return _BY_STATUS.get(state.status, CLOSED)

    # REFUNDED, PARTIALLY_REFUNDED or a status this code doesn't know: a person decides.
    return _review(session, state, "unexpected_status", now) if holding else _BY_STATUS.get(state.status, CLOSED)


def _capture_after_end(session: Session, state: _State, now: datetime) -> str:
    """A verified payment for an order that already ended (its stock was returned).

    Evidence is kept (already recorded), nothing is fulfilled and no stock moves. The order is
    put back in front of a person as ``needs_review``; if its cart already has another open
    order (only one may be open), the reason is recorded on the ended order instead, where the
    merchant command lists it.
    """
    if open_order_ref(session, state.cart_id) is None:
        _set(
            session,
            state.id,
            state.status,
            now,
            status="needs_review",
            review_reason="capture_after_end",
            status_changed_at=now,
        )
    else:
        _set(session, state.id, state.status, now, review_reason="capture_after_end")
    return NEEDS_REVIEW
