"""Ending an order's stock hold, and the cart lock while an order is open (M3.1, ADR-025).

Kept apart from ``app.domain.orders.service`` so the cart and reservation services can use it
without an import cycle. It depends only on models and the inventory primitives.

An order's hold ends only through the order, never through cart-side code:

- **Cancel** (customer) and **expiry** (the hold deadline passed) apply only before capture
  (``pending_approval``, ``approved``, ``awaiting_payment``). Once a payment may exist
  (``capturing``, ``payment_pending``, ``needs_review``) neither applies, so stock is kept.
- Both are one compare-and-set on the order's status, then one on the reservation
  (``order_hold`` -> ``released`` / ``expired``), then the stock release, in one transaction.
  Lock order: order -> reservation -> variants (ascending). They never lock a cart, like the
  reservation expiry sweep, so they can run before a cart lock is taken.
- Expiry is lazy (no worker, ADR-014/024): when the customer reads the order or the bag, before
  any change to the cart, and, bounded, before other carts reserve or add the same variants.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import NoReturn

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import mark_rollback_only, unit_of_work
from app.domain.inventory.service import InventoryInvariantViolation, release_reservation_stock
from app.domain.orders.models import OPEN_STATUSES, PRE_CAPTURE_STATUSES, Order
from app.domain.reservations.models import Reservation, ReservationItem
from app.errors import AtlasError

DEFAULT_ORDER_SWEEP_LIMIT = 20


class CheckoutInProgress(AtlasError):
    """The cart has an open order, so its contents and stock hold are fixed until it ends."""

    status_code = 409
    code = "checkout_in_progress"

    def __init__(self, public_ref: str) -> None:
        super().__init__(
            "This bag is being checked out. Cancel the order to change it.",
            details=[{"order_ref": public_ref}],
        )


def _fail(session: Session, error: AtlasError) -> NoReturn:
    mark_rollback_only(session, error.code)
    raise error


def open_order_ref(session: Session, cart_id: int) -> str | None:
    """The public reference of the cart's open order, if any. A plain read."""
    return session.execute(
        select(Order.public_ref).where(Order.cart_id == cart_id, Order.status.in_(OPEN_STATUSES))
    ).scalar_one_or_none()


def refuse_if_checkout_open(session: Session, cart_id: int) -> None:
    """Inside a cart write transaction, after the cart lock: refuse while an order is open.

    Creating an order takes the same cart lock, so an order committed while this transaction
    waited for the lock is seen here.
    """
    ref = open_order_ref(session, cart_id)
    if ref is not None:
        _fail(session, CheckoutInProgress(ref))


def end_order_hold_locked(session: Session, order_id: int, now: datetime, *, due_only: bool) -> str | None:
    """End a pre-capture order and release its hold, inside the caller's transaction.

    ``due_only`` (expiry): only if the hold deadline has passed; the order becomes ``expired``.
    Otherwise (cancellation): an order past its deadline still becomes ``expired``, any other
    ``cancelled``, mirroring the reservation release rule (§3.1 rule 7). The reservation follows:
    ``expired`` or ``released``. Returns the order's new status, or None when the compare-and-set
    matched nothing (already ended, payment in progress, or not due): nothing is released then.
    """
    query = update(Order).where(Order.id == order_id, Order.status.in_(PRE_CAPTURE_STATUSES))
    if due_only:
        query = query.where(Order.hold_expires_at <= now).values(status="expired")
    else:
        query = query.values(status=case((Order.hold_expires_at <= now, "expired"), else_="cancelled"))
    row = session.execute(
        query.values(updated_at=now, status_changed_at=now)
        .returning(Order.reservation_id, Order.status)
        .execution_options(synchronize_session=False)
    ).one_or_none()
    if row is None:
        return None
    reservation_status = "expired" if row.status == "expired" else "released"
    ended = session.execute(
        update(Reservation)
        .where(Reservation.id == row.reservation_id, Reservation.status == "order_hold")
        .values(status=reservation_status, ended_at=now)
        .returning(Reservation.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if ended is None:
        _fail(session, InventoryInvariantViolation(f"Order {order_id} does not hold its reservation."))
    release_reservation_stock(session, ended, now=now)
    return str(row.status)


def expire_order_if_due(session: Session, order_id: int, *, clock: Clock) -> bool:
    """Expire one order whose hold deadline has passed, in its own transaction. Exactly once."""
    with unit_of_work(session):
        return end_order_hold_locked(session, order_id, clock.now(), due_only=True) is not None


def expire_due_order_for_cart(session: Session, cart_id: int, *, clock: Clock) -> bool:
    """Lazy expiry for one cart: expire its open order if the hold deadline has passed."""
    with unit_of_work(session):
        due = session.execute(
            select(Order.id).where(
                Order.cart_id == cart_id,
                Order.status.in_(PRE_CAPTURE_STATUSES),
                Order.hold_expires_at <= clock.now(),
            )
        ).scalar_one_or_none()
    return due is not None and expire_order_if_due(session, due, clock=clock)


def sweep_due_orders(
    session: Session,
    *,
    variant_ids: Collection[int],
    exclude_cart_id: int | None,
    clock: Clock,
    limit: int = DEFAULT_ORDER_SWEEP_LIMIT,
) -> int:
    """Expire up to ``limit`` other carts' due orders that hold any of ``variant_ids``.

    The order counterpart of the reservation sweep: candidates in one read, then one short
    transaction each, never locking a cart. Without it, an abandoned checkout would keep its
    stock reserved until its own customer came back. Returns how many were expired.
    """
    if not variant_ids or limit <= 0:
        return 0
    with unit_of_work(session):
        now = clock.now()
        holding = select(ReservationItem.reservation_id).where(ReservationItem.variant_id.in_(sorted(variant_ids)))
        query = select(Order.id).where(
            Order.status.in_(PRE_CAPTURE_STATUSES),
            Order.hold_expires_at <= now,
            Order.reservation_id.in_(holding),
        )
        if exclude_cart_id is not None:
            query = query.where(Order.cart_id != exclude_cart_id)
        candidates = list(session.execute(query.order_by(Order.hold_expires_at, Order.id).limit(limit)).scalars())
    return sum(expire_order_if_due(session, order_id, clock=clock) for order_id in candidates)
