"""Cart-owned stock reservations (ADR-024, ARCHITECTURE §3.1).

Every public function owns its transactions: each step runs in its own ``unit_of_work``, which
commits on success and rolls back on any exception. Callers pass a session with no open
transaction and never commit. Time comes only from the injected ``Clock``; one ``now`` is taken
per transaction and compared in SQL. Values written by SQL updates are read back with
RETURNING or fresh queries; results are immutable dataclasses, never ORM objects.

Lock order (PostgreSQL, READ COMMITTED):

- ``reserve_cart`` and ``release_cart_reservation``: cart -> reservation -> variants (ascending
  id). In ``reserve_cart`` any leftover active reservation is ended, and its stock released,
  before the first variant hold.
- ``expire_reservation`` (and so ``sweep_overdue``): reservation -> variants. Never a cart.
- No transaction waits for a cart, or for an existing reservation, while holding variant locks.

Orders (M3.1, ADR-025): while the cart has an open order, ``reserve_cart`` and
``release_cart_reservation`` are refused with ``checkout_in_progress`` (checked after the cart
lock, so an order committed meanwhile is seen). A due order is expired first, in its own
transaction (order -> reservation -> variants, never a cart). Every expiry, release and sweep
here matches only ``status = 'active'``, so it never ends an order's ``order_hold``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from sqlalchemy import and_, case, insert, select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import mark_rollback_only, unit_of_work
from app.domain.cart.models import CART_IDLE_LIFETIME, Cart, CartItem
from app.domain.catalog.models import Product, Variant
from app.domain.inventory.service import StockShortfall, hold_reservation_stock, release_reservation_stock
from app.domain.orders.holds import (
    CheckoutInProgress,
    expire_due_order_for_cart,
    open_order_ref,
    refuse_if_checkout_open,
    sweep_due_orders,
)
from app.domain.reservations.models import Reservation, ReservationItem
from app.errors import AtlasError

DEFAULT_SWEEP_LIMIT = 20


# --- Results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReservationItemView:
    sku: str
    variant_id: int
    quantity: int


@dataclass(frozen=True)
class ReservationView:
    public_id: str
    status: str
    cart_version: int
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    items: tuple[ReservationItemView, ...]


@dataclass(frozen=True)
class ReserveResult:
    reservation: ReservationView
    created: bool  # False: an existing reservation for the current cart version was returned


# --- Errors (D-5: never any stock counts) ---------------------------------------------------


class CartNotFound(AtlasError):
    status_code = 404
    code = "cart_not_found"

    def __init__(self) -> None:
        super().__init__("Cart not found.")


class CartEmpty(AtlasError):
    status_code = 409
    code = "cart_empty"

    def __init__(self) -> None:
        super().__init__("The cart is empty.")


class CartHasUnavailableItems(AtlasError):
    status_code = 409
    code = "cart_has_unavailable_items"

    def __init__(self, skus: list[str]) -> None:
        super().__init__(
            "Some items in the cart are no longer available. Remove them to continue.",
            details=[{"sku": sku} for sku in skus],
        )


class ReservationShortfall(AtlasError):
    status_code = 409
    code = "insufficient_available_stock"

    def __init__(self, skus: list[str]) -> None:
        super().__init__(
            "Some items are not available in the requested quantity.",
            details=[{"sku": sku} for sku in skus],
        )


class ReservationConflict(AtlasError):
    status_code = 409
    code = "reservation_conflict"

    def __init__(self) -> None:
        super().__init__("The cart changed while it was being reserved. Please try again.")


class _LostRace(Exception):
    """Internal: the version compare-and-set changed no row (nothing was written)."""


def _fail(session: Session, error: AtlasError) -> NoReturn:
    """Raise from inside a write transaction that may already have changed rows."""
    mark_rollback_only(session, error.code)
    raise error


# --- Reads -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CartState:
    version: int
    reservation_id: int | None
    reservation_version: int | None
    reservation_expires_at: datetime | None

    def matching_reservation(self, now: datetime) -> int | None:
        """The active reservation if it is unexpired and made for the current version."""
        if (
            self.reservation_id is not None
            and self.reservation_version == self.version
            and self.reservation_expires_at is not None
            and self.reservation_expires_at > now
        ):
            return self.reservation_id
        return None

    def overdue_reservation(self, now: datetime) -> int | None:
        if self.reservation_id is not None and self.reservation_expires_at is not None:
            return self.reservation_id if self.reservation_expires_at <= now else None
        return None


def _cart_state(session: Session, cart_id: int, now: datetime) -> _CartState:
    """The cart's version and its active reservation, in ONE statement (§3.1 rule 2).

    Raises CartNotFound for an unknown cart or one past its 30-day expiry.
    """
    row = session.execute(
        select(
            Cart.version,
            Cart.expires_at.label("cart_expires_at"),
            Reservation.id.label("reservation_id"),
            Reservation.cart_version.label("reservation_version"),
            Reservation.expires_at.label("reservation_expires_at"),
        )
        .select_from(Cart)
        .outerjoin(Reservation, and_(Reservation.cart_id == Cart.id, Reservation.status == "active"))
        .where(Cart.id == cart_id)
    ).one_or_none()
    if row is None or row.cart_expires_at <= now:
        raise CartNotFound()
    return _CartState(row.version, row.reservation_id, row.reservation_version, row.reservation_expires_at)


def _view(session: Session, reservation_id: int) -> ReservationView:
    header = session.execute(
        select(
            Reservation.public_id,
            Reservation.status,
            Reservation.cart_version,
            Reservation.created_at,
            Reservation.expires_at,
            Reservation.ended_at,
        ).where(Reservation.id == reservation_id)
    ).one()
    items = session.execute(
        select(Variant.sku, ReservationItem.variant_id, ReservationItem.quantity)
        .join(Variant, Variant.id == ReservationItem.variant_id)
        .where(ReservationItem.reservation_id == reservation_id)
        .order_by(ReservationItem.variant_id)
    ).all()
    return ReservationView(
        public_id=header.public_id,
        status=header.status,
        cart_version=header.cart_version,
        created_at=header.created_at,
        expires_at=header.expires_at,
        ended_at=header.ended_at,
        items=tuple(ReservationItemView(sku, variant_id, quantity) for sku, variant_id, quantity in items),
    )


@dataclass(frozen=True)
class _Line:
    variant_id: int
    sku: str
    quantity: int
    available: bool  # variant and product both active


def _lines(session: Session, cart_id: int) -> list[_Line]:
    rows = session.execute(
        select(CartItem.variant_id, Variant.sku, CartItem.quantity, Variant.status, Product.status.label("p_status"))
        .join(Variant, Variant.id == CartItem.variant_id)
        .join(Product, Product.id == Variant.product_id)
        .where(CartItem.cart_id == cart_id)
        .order_by(CartItem.variant_id)
    ).all()
    return [
        _Line(row.variant_id, row.sku, row.quantity, row.status == "active" and row.p_status == "active")
        for row in rows
    ]


# --- Ending reservations -------------------------------------------------------------------


def _lock_variants(session: Session, variant_ids: Collection[int]) -> None:
    """Lock variant rows in ascending id order (PostgreSQL ``FOR NO KEY UPDATE``).

    The same lock strength the stock UPDATEs take, so it never conflicts with the KEY SHARE
    locks that foreign-key checks take. SQLite has no row locks (one writer); the clause is
    omitted there.
    """
    if variant_ids:
        session.execute(
            select(Variant.id)
            .where(Variant.id.in_(sorted(variant_ids)))
            .order_by(Variant.id)
            .with_for_update(key_share=True)
        ).all()


def _end_active_reservation_locked(
    session: Session, cart_id: int, now: datetime, *, then_hold: Collection[int] = ()
) -> tuple[int, str] | None:
    """End the cart's active reservation, if any, and release its stock. Caller holds the cart lock.

    One compare-and-set decides the outcome atomically: overdue reservations become
    ``expired`` (released with ``reservation_expiry``), others ``released``. No row means
    nothing was active, so nothing is released. Lock order: reservation, then variants.

    ``then_hold``: variants the same transaction will hold next. They are locked together
    with the released reservation's variants, in one ascending pass, before the release. Two
    separate ascending passes (release, then hold) could lock 9 then 3 and deadlock against a
    concurrent transaction locking 3 then 9.
    Returns (reservation id, new status).
    """
    row = session.execute(
        update(Reservation)
        .where(Reservation.cart_id == cart_id, Reservation.status == "active")
        .values(
            status=case((Reservation.expires_at <= now, "expired"), else_="released"),
            ended_at=now,
        )
        .returning(Reservation.id, Reservation.status)
        .execution_options(synchronize_session=False)
    ).one_or_none()
    if row is None:
        return None
    if then_hold:
        held = session.execute(
            select(ReservationItem.variant_id).where(ReservationItem.reservation_id == row.id)
        ).scalars()
        _lock_variants(session, set(held) | set(then_hold))
    release_reservation_stock(session, row.id, now=now)
    return row.id, row.status


def expire_reservation(session: Session, reservation_id: int, *, clock: Clock) -> bool:
    """Expire one overdue reservation and release its stock, in its own transaction.

    The compare-and-set (``status = 'active' AND expires_at <= :now``) lets exactly one
    transaction end the reservation; only that transaction releases stock. Returns False if
    the reservation was already ended or isn't due. Locks the reservation, then its variants;
    never a cart.
    """
    with unit_of_work(session):
        now = clock.now()
        expired_id = session.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id, Reservation.status == "active", Reservation.expires_at <= now)
            .values(status="expired", ended_at=now)
            .returning(Reservation.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if expired_id is None:
            return False
        release_reservation_stock(session, expired_id, now=now)
    return True


def sweep_overdue(
    session: Session,
    *,
    variant_ids: Collection[int],
    exclude_cart_id: int | None,
    clock: Clock,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> int:
    """Expire up to ``limit`` overdue reservations that hold any of ``variant_ids``.

    Candidates are chosen in one read (oldest ``expires_at`` first, then id), then each is
    expired by ``expire_reservation`` in its own short transaction, so no reservation lock is
    held while another is taken, and no cart is ever locked. A failure propagates. Returns
    how many were expired by this call.
    """
    if not variant_ids or limit <= 0:
        return 0
    with unit_of_work(session):
        now = clock.now()
        holding = select(ReservationItem.reservation_id).where(ReservationItem.variant_id.in_(sorted(variant_ids)))
        query = select(Reservation.id).where(
            Reservation.status == "active",
            Reservation.expires_at <= now,
            Reservation.id.in_(holding),
        )
        if exclude_cart_id is not None:
            query = query.where(Reservation.cart_id != exclude_cart_id)
        candidates = list(
            session.execute(query.order_by(Reservation.expires_at, Reservation.id).limit(limit)).scalars()
        )
    return sum(expire_reservation(session, reservation_id, clock=clock) for reservation_id in candidates)


# --- Customer operations ---------------------------------------------------------------------


def active_reservation(session: Session, cart_id: int, *, clock: Clock) -> ReservationView | None:
    """The cart's active, unexpired reservation, or None.

    **This read can write:** an overdue active reservation is expired, and its stock released,
    in a separate transaction (lazy expiry, ADR-024 D6), and None is returned.
    """
    with unit_of_work(session):
        now = clock.now()
        row = session.execute(
            select(Reservation.id, Reservation.expires_at).where(
                Reservation.cart_id == cart_id, Reservation.status == "active"
            )
        ).one_or_none()
        if row is None:
            return None
        if row.expires_at > now:
            return _view(session, row.id)
        overdue_id = row.id
    expire_reservation(session, overdue_id, clock=clock)
    return None


def release_cart_reservation(session: Session, cart_id: int, *, clock: Clock) -> ReservationView | None:
    """Release the cart's active reservation on the customer's request (§3.1 rule 7).

    One transaction: take the cart lock by advancing its version (also when nothing turns out
    to be active, decision B), then end the active reservation with the CASE compare-and-set
    and release its stock. An overdue reservation ends as ``expired``. Returns the ended
    reservation, or None when nothing was active or the cart doesn't exist. Repeating it
    never releases stock twice. The cart is locked even past its 30-day expiry, so stock
    held for an expired cart can still be released. Refused with ``checkout_in_progress``
    while the cart has an open order: its hold ends only through the order.
    """
    expire_due_order_for_cart(session, cart_id, clock=clock)
    with unit_of_work(session):
        now = clock.now()
        locked = session.execute(
            update(Cart)
            .where(Cart.id == cart_id)
            .values(version=Cart.version + 1)
            .returning(Cart.version)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if locked is None:
            return None
        refuse_if_checkout_open(session, cart_id)
        ended = _end_active_reservation_locked(session, cart_id, now)
        if ended is None:
            return None
        return _view(session, ended[0])


def reserve_cart(
    session: Session,
    cart_id: int,
    *,
    clock: Clock,
    ttl: timedelta,
    sweep_limit: int = DEFAULT_SWEEP_LIMIT,
) -> ReserveResult:
    """Reserve the cart's current contents for ``ttl`` (ADR-024, §3.1 rules 2-5, 8).

    0. The cart's own due order, if any, is expired in its own transaction; a cart with an
       open order is refused with ``checkout_in_progress`` (ADR-025), before and after the lock.
    1. Read (one statement): an active, unexpired reservation for the current version is
       returned as is (``created=False``); nothing is written.
    2. The cart's own overdue reservation is expired in its own transaction.
    3. Overdue reservations, and due orders' holds (ADR-025), of other carts holding these
       variants are swept, one per transaction, before the cart lock.
    4. Main transaction: version compare-and-set (cart lock; also refreshes the cart's 30-day
       expiry, decision C), end any leftover active reservation **before** the first variant
       hold, re-read the lines, reject empty or unavailable carts, insert the reservation and
       its immutable items, hold every variant in ascending order. Any failure rolls back
       everything, including the version change and the leftover's release.
    5. Lost race: re-read; return the matching reservation (``created=False``) or raise
       ReservationConflict.
    """
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")

    expire_due_order_for_cart(session, cart_id, clock=clock)
    with unit_of_work(session):
        now = clock.now()
        state = _cart_state(session, cart_id, now)
        checkout = open_order_ref(session, cart_id)
        if checkout is not None:
            raise CheckoutInProgress(checkout)  # nothing written
        matching = state.matching_reservation(now)
        if matching is not None:
            return ReserveResult(_view(session, matching), created=False)
        overdue = state.overdue_reservation(now)
        variant_ids = list(session.execute(select(CartItem.variant_id).where(CartItem.cart_id == cart_id)).scalars())

    if overdue is not None:
        expire_reservation(session, overdue, clock=clock)
    sweep_overdue(session, variant_ids=variant_ids, exclude_cart_id=cart_id, clock=clock, limit=sweep_limit)
    sweep_due_orders(session, variant_ids=variant_ids, exclude_cart_id=cart_id, clock=clock, limit=sweep_limit)

    try:
        with unit_of_work(session):
            reservation_id = _reserve_locked(session, cart_id, seen=state.version, now=clock.now(), ttl=ttl)
            view = _view(session, reservation_id)
        return ReserveResult(view, created=True)
    except _LostRace:
        pass

    with unit_of_work(session):
        now = clock.now()
        matching = _cart_state(session, cart_id, now).matching_reservation(now)
        if matching is not None:
            return ReserveResult(_view(session, matching), created=False)
    raise ReservationConflict()


def _reserve_locked(session: Session, cart_id: int, *, seen: int, now: datetime, ttl: timedelta) -> int:
    """The main reservation transaction's work. Returns the new reservation's id."""
    version = session.execute(
        update(Cart)
        .where(Cart.id == cart_id, Cart.version == seen, Cart.expires_at > now)
        .values(version=Cart.version + 1, expires_at=now + CART_IDLE_LIFETIME)
        .returning(Cart.version)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if version is None:
        raise _LostRace()
    refuse_if_checkout_open(session, cart_id)

    lines = _lines(session, cart_id)  # re-read after the lock (rule 6)
    if not lines:
        _fail(session, CartEmpty())
    unavailable = [line.sku for line in lines if not line.available]
    if unavailable:
        _fail(session, CartHasUnavailableItems(unavailable))

    # End any leftover active reservation before the first variant hold, so no variant lock is
    # ever held while waiting for an existing reservation; its variants and the new lines'
    # variants are locked in one ascending pass (cart -> reservation -> variants).
    _end_active_reservation_locked(session, cart_id, now, then_hold=[line.variant_id for line in lines])

    reservation_id = session.execute(
        insert(Reservation)
        .values(
            public_id=uuid.uuid4().hex,
            cart_id=cart_id,
            cart_version=version,
            status="active",
            created_at=now,
            expires_at=now + ttl,
        )
        .returning(Reservation.id)
    ).scalar_one()
    session.execute(
        insert(ReservationItem),
        [
            {"reservation_id": reservation_id, "variant_id": line.variant_id, "quantity": line.quantity}
            for line in lines
        ],
    )
    try:
        hold_reservation_stock(session, reservation_id, now=now)
    except StockShortfall as shortfall:
        # The session is already rollback-only; name every short line by SKU, never a count.
        short = set(shortfall.variant_ids)
        raise ReservationShortfall([line.sku for line in lines if line.variant_id in short]) from None
    return reservation_id
