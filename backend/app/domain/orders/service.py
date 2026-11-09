"""Order creation, reading and cancellation (M3.1, ADR-008, ADR-025).

Every public function owns its transactions through ``unit_of_work`` and takes a session with
no open transaction; time comes only from the injected ``Clock``. Results are immutable
dataclasses. Every function takes the caller's cart id: an order is found only by its public
reference **and** the cart it belongs to, so a reference alone never grants access.

Creating an order, in one transaction (lock order cart -> order -> reservation; no variant is
locked because no stock moves):

1. advance the cart version with a compare-and-set against the version read before (the cart
   lock; also refreshes the cart's 30-day expiry);
2. re-read the cart's active reservation: it must be unexpired and made for that version;
3. re-read the lines: they must equal the reservation's items, and every variant and product
   must still be active;
4. quote shipping, compute the totals in USD cents and the version hash;
5. insert the order (``pending_approval``, hold deadline ``now + ORDER_HOLD_MINUTES``) and its
   items, then adopt the reservation (``active`` -> ``order_hold``) with a compare-and-set.

The reservation's stock stays reserved throughout: nothing is held twice and no movement is
written. Any failure rolls all of it back.

Recovery: creating an order for a cart that already has an open order returns that order
(``created=False``), so a client that timed out after the first request committed can retry
and get the same reference. The cart also reports its open order (``checkout_summary``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import mark_rollback_only, unit_of_work
from app.domain.cart.models import CART_IDLE_LIFETIME, Cart, CartItem
from app.domain.catalog.models import Product, Variant
from app.domain.catalog.service import product_names
from app.domain.catalog.vocab import Language
from app.domain.orders.holds import end_order_hold_locked, expire_due_order_for_cart, open_order_ref
from app.domain.orders.models import OPEN_STATUSES, PAYMENT_IN_PROGRESS_STATUSES, Order, OrderItem
from app.domain.orders.snapshot import SnapshotLine, version_hash
from app.domain.reservations.models import Reservation, ReservationItem
from app.domain.reservations.service import CartHasUnavailableItems, _cart_state
from app.domain.shipping.service import normalize_country, quote
from app.errors import AtlasError

ORDER_CURRENCY = "USD"  # ADR-003


# --- Results -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderLineView:
    sku: str
    product_slug: str
    name_snapshot: str
    size: str
    colour: str
    unit_price_minor: int
    quantity: int
    line_total_minor: int


@dataclass(frozen=True)
class OrderView:
    public_ref: str
    status: str
    version_hash: str
    currency: str
    subtotal_minor: int
    shipping_minor: int
    total_minor: int
    ship_country: str
    shipping_zone: str
    lang: str
    hold_expires_at: datetime
    created_at: datetime
    status_changed_at: datetime
    lines: tuple[OrderLineView, ...]


@dataclass(frozen=True)
class CreateOrderResult:
    order: OrderView
    created: bool  # False: the cart's existing open order was returned


@dataclass(frozen=True)
class CheckoutSummary:
    public_ref: str
    status: str


# --- Errors ------------------------------------------------------------------------------------


class OrderNotFound(AtlasError):
    """No such order for this cart. Also the answer for another cart's order (no oracle)."""

    status_code = 404
    code = "order_not_found"

    def __init__(self) -> None:
        super().__init__("Order not found.")


class ReservationRequired(AtlasError):
    status_code = 409
    code = "reservation_required"

    def __init__(self) -> None:
        super().__init__("Reserve the items in your bag before checking out.")


class CheckoutConflict(AtlasError):
    status_code = 409
    code = "checkout_conflict"

    def __init__(self) -> None:
        super().__init__("The bag changed while the order was being created. Please review it again.")


class OrderNotCancellable(AtlasError):
    status_code = 409
    code = "order_not_cancellable"

    def __init__(self, status: str) -> None:
        super().__init__("This order can no longer be cancelled.", details=[{"status": status}])


class PaymentInProgress(AtlasError):
    """A payment may exist: the hold is kept and the order can't be cancelled here (ADR-025)."""

    status_code = 409
    code = "payment_in_progress"

    def __init__(self) -> None:
        super().__init__("A payment for this order is in progress, so it can't be cancelled now.")


class OrderInvariantViolation(AtlasError):
    """Data the order rules forbid (for example a non-USD price). A bug: generic 500 to clients."""

    status_code = 500
    code = "order_invariant_violation"


class _LostRace(Exception):
    """Internal: the cart version compare-and-set changed no row (nothing was written)."""


def _fail(session: Session, error: AtlasError) -> NoReturn:
    mark_rollback_only(session, error.code)
    raise error


# --- Reads -------------------------------------------------------------------------------------


def _order_view(session: Session, order_id: int) -> OrderView:
    order = session.execute(
        select(
            Order.public_ref,
            Order.status,
            Order.version_hash,
            Order.currency,
            Order.subtotal_minor,
            Order.shipping_minor,
            Order.total_minor,
            Order.ship_country,
            Order.shipping_zone,
            Order.lang,
            Order.hold_expires_at,
            Order.created_at,
            Order.status_changed_at,
        ).where(Order.id == order_id)
    ).one()
    items = session.execute(
        select(
            OrderItem.sku,
            OrderItem.product_slug,
            OrderItem.name_snapshot,
            OrderItem.size,
            OrderItem.colour,
            OrderItem.unit_price_minor,
            OrderItem.quantity,
            OrderItem.line_total_minor,
        )
        .where(OrderItem.order_id == order_id)
        .order_by(OrderItem.sku)
    ).all()
    return OrderView(
        public_ref=order.public_ref,
        status=order.status,
        version_hash=order.version_hash,
        currency=order.currency,
        subtotal_minor=order.subtotal_minor,
        shipping_minor=order.shipping_minor,
        total_minor=order.total_minor,
        ship_country=order.ship_country,
        shipping_zone=order.shipping_zone,
        lang=order.lang,
        hold_expires_at=order.hold_expires_at,
        created_at=order.created_at,
        status_changed_at=order.status_changed_at,
        lines=tuple(OrderLineView(*item) for item in items),
    )


def _owned_order_id(session: Session, cart_id: int, public_ref: str) -> int:
    order_id = session.execute(
        select(Order.id).where(Order.cart_id == cart_id, Order.public_ref == public_ref)
    ).scalar_one_or_none()
    if order_id is None:
        raise OrderNotFound()
    return order_id


def _open_order_id(session: Session, cart_id: int) -> int | None:
    ref = open_order_ref(session, cart_id)
    return None if ref is None else _owned_order_id(session, cart_id, ref)


def get_order(session: Session, cart_id: int, public_ref: str, *, clock: Clock) -> OrderView:
    """The cart's order with this reference. **Can write:** a due hold is expired first (lazy)."""
    expire_due_order_for_cart(session, cart_id, clock=clock)
    with unit_of_work(session):
        return _order_view(session, _owned_order_id(session, cart_id, public_ref))


def checkout_summary(session: Session, cart_id: int, *, clock: Clock) -> CheckoutSummary | None:
    """The cart's open order, if any, for the bag. **Can write:** a due hold is expired first."""
    expire_due_order_for_cart(session, cart_id, clock=clock)
    with unit_of_work(session):
        row = session.execute(
            select(Order.public_ref, Order.status).where(Order.cart_id == cart_id, Order.status.in_(OPEN_STATUSES))
        ).one_or_none()
    return None if row is None else CheckoutSummary(public_ref=row.public_ref, status=row.status)


# --- Cancellation ------------------------------------------------------------------------------


def cancel_order(session: Session, cart_id: int, public_ref: str, *, clock: Clock) -> OrderView:
    """Cancel the cart's order before capture and release its hold, in one transaction.

    Idempotent: cancelling an order that is already cancelled or expired returns it unchanged.
    An order past its hold deadline ends as ``expired``, not ``cancelled``. While a payment may
    exist (``capturing``, ``payment_pending``, ``needs_review``) it is refused with
    ``payment_in_progress`` and the hold is kept; a paid or failed order is not cancellable.
    """
    with unit_of_work(session):
        order_id = _owned_order_id(session, cart_id, public_ref)
        if end_order_hold_locked(session, order_id, clock.now(), due_only=False) is None:
            status = session.execute(select(Order.status).where(Order.id == order_id)).scalar_one()
            if status in PAYMENT_IN_PROGRESS_STATUSES:
                raise PaymentInProgress()
            if status not in ("cancelled", "expired"):
                raise OrderNotCancellable(status)
        return _order_view(session, order_id)


# --- Creation ----------------------------------------------------------------------------------


def create_order(
    session: Session,
    cart_id: int,
    *,
    ship_country: str,
    lang: Language,
    clock: Clock,
    hold: timedelta,
) -> CreateOrderResult:
    """Create an order from the cart's reserved contents, adopting the reservation (see module doc).

    Raises CartNotFound, ReservationRequired (no active, unexpired reservation for the current
    cart version), CartHasUnavailableItems, InvalidCountryCode, UnsupportedDestination or
    CheckoutConflict (the cart changed meanwhile). Returns the cart's open order instead of
    creating a second one.
    """
    if hold <= timedelta(0):
        raise ValueError("hold must be positive")
    country = normalize_country(ship_country)  # 422 before anything is read or written

    expire_due_order_for_cart(session, cart_id, clock=clock)
    with unit_of_work(session):
        now = clock.now()
        state = _cart_state(session, cart_id, now)
        existing = _open_order_id(session, cart_id)
        if existing is not None:
            return CreateOrderResult(_order_view(session, existing), created=False)
        reservation_id = state.matching_reservation(now)
        if reservation_id is None:
            raise ReservationRequired()

    try:
        with unit_of_work(session):
            order_id = _create_locked(
                session,
                cart_id,
                seen=state.version,
                reservation_id=reservation_id,
                now=clock.now(),
                country=country,
                lang=lang,
                hold=hold,
            )
            view = _order_view(session, order_id)
        return CreateOrderResult(view, created=True)
    except _LostRace:
        pass

    # Lost the cart lock race: a concurrent request may have created the order (recovery).
    with unit_of_work(session):
        existing = _open_order_id(session, cart_id)
        if existing is not None:
            return CreateOrderResult(_order_view(session, existing), created=False)
    raise CheckoutConflict()


@dataclass(frozen=True)
class _Line:
    variant_id: int
    sku: str
    product_slug: str
    size: str
    colour: str
    quantity: int
    price_minor: int
    currency: str
    available: bool


def _create_locked(
    session: Session,
    cart_id: int,
    *,
    seen: int,
    reservation_id: int,
    now: datetime,
    country: str,
    lang: Language,
    hold: timedelta,
) -> int:
    locked = session.execute(
        update(Cart)
        .where(Cart.id == cart_id, Cart.version == seen, Cart.expires_at > now)
        .values(version=Cart.version + 1, updated_at=now, expires_at=now + CART_IDLE_LIFETIME)
        .returning(Cart.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if locked is None:
        raise _LostRace()

    # Fresh reads after the lock (§3.1 rule 6).
    reservation = session.execute(
        select(Reservation.id, Reservation.cart_version, Reservation.expires_at).where(
            Reservation.cart_id == cart_id, Reservation.status == "active"
        )
    ).one_or_none()
    if (
        reservation is None
        or reservation.id != reservation_id
        or reservation.cart_version != seen
        or reservation.expires_at <= now
    ):
        _fail(session, ReservationRequired())

    rows = session.execute(
        select(
            CartItem.variant_id,
            Variant.sku,
            Product.slug,
            Variant.size,
            Variant.colour,
            CartItem.quantity,
            Variant.price_minor,
            Variant.currency,
            Variant.status,
            Product.status.label("product_status"),
        )
        .join(Variant, Variant.id == CartItem.variant_id)
        .join(Product, Product.id == Variant.product_id)
        .where(CartItem.cart_id == cart_id)
        .order_by(CartItem.variant_id)
    ).all()
    lines = [
        _Line(
            variant_id=row.variant_id,
            sku=row.sku,
            product_slug=row.slug,
            size=row.size,
            colour=row.colour,
            quantity=row.quantity,
            price_minor=row.price_minor,
            currency=row.currency,
            available=row.status == "active" and row.product_status == "active",
        )
        for row in rows
    ]
    held = {
        (row.variant_id, row.quantity)
        for row in session.execute(
            select(ReservationItem.variant_id, ReservationItem.quantity).where(
                ReservationItem.reservation_id == reservation_id
            )
        )
    }
    if not lines or {(line.variant_id, line.quantity) for line in lines} != held:
        _fail(session, CheckoutConflict())  # the reservation no longer matches the cart
    unavailable = [line.sku for line in lines if not line.available]
    if unavailable:
        _fail(session, CartHasUnavailableItems(unavailable))
    if any(line.currency != ORDER_CURRENCY for line in lines):
        _fail(session, OrderInvariantViolation("A cart line is not priced in USD."))

    try:
        shipping = quote(session, country)
    except AtlasError as error:
        _fail(session, error)
    if shipping.currency != ORDER_CURRENCY:
        _fail(session, OrderInvariantViolation("The shipping rate is not priced in USD."))

    english = product_names(session, {line.product_slug for line in lines}, "en")
    subtotal = sum(line.price_minor * line.quantity for line in lines)
    total = subtotal + shipping.amount_minor
    public_ref = uuid.uuid4().hex
    digest = version_hash(
        public_ref=public_ref,
        currency=ORDER_CURRENCY,
        ship_country=country,
        lines=[
            SnapshotLine(line.sku, line.quantity, line.price_minor, line.price_minor * line.quantity) for line in lines
        ],
        subtotal_minor=subtotal,
        shipping_minor=shipping.amount_minor,
        total_minor=total,
    )

    order_id = session.execute(
        insert(Order)
        .values(
            public_ref=public_ref,
            cart_id=cart_id,
            reservation_id=reservation_id,
            status="pending_approval",
            version_hash=digest,
            currency=ORDER_CURRENCY,
            subtotal_minor=subtotal,
            shipping_minor=shipping.amount_minor,
            total_minor=total,
            ship_country=country,
            shipping_zone=shipping.zone_code,
            lang=lang,
            hold_expires_at=now + hold,
            created_at=now,
            updated_at=now,
            status_changed_at=now,
        )
        .returning(Order.id)
    ).scalar_one()
    session.execute(
        insert(OrderItem),
        [
            {
                "order_id": order_id,
                "variant_id": line.variant_id,
                "sku": line.sku,
                "product_slug": line.product_slug,
                "name_snapshot": english[line.product_slug].name,
                "size": line.size,
                "colour": line.colour,
                "unit_price_minor": line.price_minor,
                "quantity": line.quantity,
                "line_total_minor": line.price_minor * line.quantity,
            }
            for line in lines
        ],
    )

    adopted = session.execute(
        update(Reservation)
        .where(
            Reservation.id == reservation_id,
            Reservation.status == "active",
            Reservation.cart_version == seen,
            Reservation.expires_at > now,
        )
        .values(status="order_hold")
        .returning(Reservation.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if adopted is None:
        _fail(session, ReservationRequired())  # expired or ended concurrently (e.g. lazy expiry)
    return order_id
