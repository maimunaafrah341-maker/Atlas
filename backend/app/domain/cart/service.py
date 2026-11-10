"""Anonymous cart writes (ADR-024, ARCHITECTURE §3.1 rules 1, 6, 8 and 11).

Each public function owns its transactions through ``unit_of_work`` (commit on success, roll
back on any exception); callers pass a session with no open transaction. Time comes only from
the injected ``Clock``, one ``now`` per transaction. Results are immutable dataclasses.

An edit, in one transaction:
1. advance ``carts.version`` and refresh the 30-day expiry (the cart lock);
2. end the cart's active reservation, if any, and release its stock (reservation, then its
   variants in ascending order), reusing the reservation service's compare-and-set;
3. re-read the variant and product, validate, and run the advisory availability check, which
   sees the stock just released but holds nothing;
4. write the line.

Any failure rolls all of it back, so an invalid edit never releases the customer's
reservation. While the cart has an open order (M3.1, ADR-025) every edit is refused with
``checkout_in_progress``, checked right after the cart lock; a due order is expired first, in
its own transaction. Before the cart lock, overdue reservations of other carts holding the variant are
swept (one short transaction each, never locking a cart).

Tokens: the raw cart token exists only in memory, returned once when a cart is created; only
its SHA-256 hex digest is stored. Neither is ever logged.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import mark_rollback_only, unit_of_work
from app.domain.cart.models import CART_IDLE_LIFETIME, MAX_CART_LINES, MAX_QUANTITY_PER_LINE, Cart, CartItem
from app.domain.catalog.models import Product, Variant
from app.domain.catalog.service import ProductName, product_names
from app.domain.catalog.vocab import Language
from app.domain.inventory.service import InsufficientAvailableStock, InvalidQuantity, VariantInactive, VariantNotFound
from app.domain.orders.holds import expire_due_order_for_cart, refuse_if_checkout_open, sweep_due_orders
from app.domain.reservations.models import Reservation
from app.domain.reservations.service import (
    DEFAULT_SWEEP_LIMIT,
    CartNotFound,
    _end_active_reservation_locked,  # the one shared CASE compare-and-set (§3.1 rule 7)
    sweep_overdue,
)
from app.errors import AtlasError

TOKEN_BYTES = 32  # 256 bits; secrets.token_urlsafe gives 43 characters
MAX_TOKEN_LENGTH = 128  # anything longer is not one of ours; don't hash arbitrary input


# --- Results -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CartLineView:
    """A cart line with live catalogue data. Never a stock count (D-5)."""

    sku: str
    product_slug: str
    size: str
    colour: str
    quantity: int
    unit_price_minor: int
    currency: str
    available: bool  # False when the variant or its product has been archived


@dataclass(frozen=True)
class CartView:
    cart_id: int  # internal: the HTTP layer identifies carts by token, never by id
    version: int
    expires_at: datetime
    lines: tuple[CartLineView, ...]


@dataclass(frozen=True)
class EndedReservation:
    public_id: str
    status: str  # "released", or "expired" if it was already overdue


@dataclass(frozen=True)
class CartChange:
    cart: CartView
    ended_reservation: EndedReservation | None  # the active reservation this edit ended
    # The raw token of a cart created by this call, returned exactly once so the HTTP layer can
    # set the cookie. Excluded from repr so it can't leak into logs or tracebacks.
    new_token: str | None = field(default=None, repr=False)


# --- Errors ------------------------------------------------------------------------------------


class CartLineLimit(AtlasError):
    status_code = 409
    code = "cart_line_limit"

    def __init__(self) -> None:
        super().__init__(
            f"A cart can hold at most {MAX_CART_LINES} different items.",
            details=[{"limit": MAX_CART_LINES}],
        )


class _NoChange(Exception):
    """Internal: nothing to change; roll back the version advance and return the cart as is."""


def _fail(session: Session, error: AtlasError) -> NoReturn:
    """Raise from inside a write transaction that may already have changed rows."""
    mark_rollback_only(session, error.code)
    raise error


# --- Tokens and lookup -------------------------------------------------------------------------


def new_cart_token() -> tuple[str, str]:
    """A fresh random cart token and its SHA-256 hex digest: (raw token, digest)."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, _digest(token)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_cart(session: Session, token: str, *, clock: Clock) -> int | None:
    """The id of the unexpired cart for this raw token, or None. Reading never extends expiry.

    The lookup is by digest in the database (a unique index), so no token is ever compared in
    application code.
    """
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    with unit_of_work(session):
        return session.execute(
            select(Cart.id).where(Cart.token_hash == _digest(token), Cart.expires_at > clock.now())
        ).scalar_one_or_none()


# --- Reads -------------------------------------------------------------------------------------


def _cart_view(session: Session, cart_id: int) -> CartView:
    header = session.execute(select(Cart.version, Cart.expires_at).where(Cart.id == cart_id)).one()
    rows = session.execute(
        select(
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
        .order_by(CartItem.created_at, CartItem.variant_id)
    ).all()
    return CartView(
        cart_id=cart_id,
        version=header.version,
        expires_at=header.expires_at,
        lines=tuple(
            CartLineView(
                sku=row.sku,
                product_slug=row.slug,
                size=row.size,
                colour=row.colour,
                quantity=row.quantity,
                unit_price_minor=row.price_minor,
                currency=row.currency,
                available=row.status == "active" and row.product_status == "active",
            )
            for row in rows
        ),
    )


@dataclass(frozen=True)
class NamedCart:
    cart: CartView
    names: dict[str, ProductName]  # by product slug, in the requested language


def read_cart(session: Session, cart_id: int, *, lang: Language) -> NamedCart:
    """The cart's lines with live prices and product names in ``lang``, in one read-only
    transaction. Never changes the cart's version or expiry, and never touches reservations.
    """
    with unit_of_work(session):
        view = _cart_view(session, cart_id)
        names = product_names(session, {line.product_slug for line in view.lines}, lang)
    return NamedCart(cart=view, names=names)


def _line_quantity(session: Session, cart_id: int, sku: str) -> int | None:
    return session.execute(
        select(CartItem.quantity)
        .join(Variant, Variant.id == CartItem.variant_id)
        .where(CartItem.cart_id == cart_id, Variant.sku == sku)
    ).scalar_one_or_none()


def _variant_id(session: Session, sku: str) -> int | None:
    return session.execute(select(Variant.id).where(Variant.sku == sku)).scalar_one_or_none()


def _sweep_for(session: Session, sku: str, cart_id: int | None, clock: Clock) -> None:
    """Before the cart lock: expire other carts' overdue holds and due orders on this variant
    (bounded), and this cart's own due order (ADR-025). Each in its own short transaction;
    never a cart lock."""
    with unit_of_work(session):
        variant_id = _variant_id(session, sku)
    if variant_id is not None:
        sweep_overdue(
            session, variant_ids=[variant_id], exclude_cart_id=cart_id, clock=clock, limit=DEFAULT_SWEEP_LIMIT
        )
        sweep_due_orders(
            session, variant_ids=[variant_id], exclude_cart_id=cart_id, clock=clock, limit=DEFAULT_SWEEP_LIMIT
        )
    if cart_id is not None:
        expire_due_order_for_cart(session, cart_id, clock=clock)


# --- Locked steps ------------------------------------------------------------------------------


def _lock_cart(session: Session, cart_id: int, now: datetime) -> None:
    """Rule 1: advance the version (the cart lock) and refresh the 30-day expiry."""
    locked = session.execute(
        update(Cart)
        .where(Cart.id == cart_id, Cart.expires_at > now)
        .values(version=Cart.version + 1, updated_at=now, expires_at=now + CART_IDLE_LIFETIME)
        .returning(Cart.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if locked is None:
        raise CartNotFound()  # nothing written yet


def _end_reservation(session: Session, cart_id: int, now: datetime) -> EndedReservation | None:
    ended = _end_active_reservation_locked(session, cart_id, now)
    if ended is None:
        return None
    reservation_id, status = ended
    public_id = session.execute(select(Reservation.public_id).where(Reservation.id == reservation_id)).scalar_one()
    return EndedReservation(public_id=public_id, status=status)


def _validate_quantity(quantity: Any) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise InvalidQuantity("Quantity must be a whole number.", details=[{"field": "quantity"}])
    if not 1 <= quantity <= MAX_QUANTITY_PER_LINE:
        raise InvalidQuantity(
            f"Quantity must be between 1 and {MAX_QUANTITY_PER_LINE}.",
            details=[{"field": "quantity", "min": 1, "max": MAX_QUANTITY_PER_LINE}],
        )
    return quantity


# --- Writes ------------------------------------------------------------------------------------


def set_quantity(session: Session, cart_id: int | None, sku: str, quantity: int, *, clock: Clock) -> CartChange:
    """Set a line's quantity (replace, not add), creating the cart when ``cart_id`` is None.

    One transaction: (create the cart,) lock it, end and release its active reservation, then
    re-read the variant and product, check that both are active, the 20-line limit and the
    advisory availability (after the release, holding nothing), and upsert the line. Any
    failure rolls everything back, including a newly created cart, the version advance and
    the reservation release.

    Setting a line to the quantity it already has is a no-op (a repeated PUT): decided under
    the cart lock but **before** the reservation is touched, then rolled back, so the version,
    expiry, reservation, stock and lines are all left exactly as they were.
    """
    quantity = _validate_quantity(quantity)
    _sweep_for(session, sku, cart_id, clock)
    try:
        return _set_quantity(session, cart_id, sku, quantity, clock)
    except _NoChange:
        pass
    if cart_id is None:  # unreachable: creating a cart is always a change
        raise RuntimeError("no-op without a cart")
    with unit_of_work(session):
        return CartChange(cart=_cart_view(session, cart_id), ended_reservation=None)


def _set_quantity(session: Session, cart_id: int | None, sku: str, quantity: int, clock: Clock) -> CartChange:
    with unit_of_work(session):
        now = clock.now()
        new_token: str | None = None
        if cart_id is None:
            new_token, digest = new_cart_token()
            cart_id = session.execute(
                insert(Cart)
                .values(
                    token_hash=digest, version=0, created_at=now, updated_at=now, expires_at=now + CART_IDLE_LIFETIME
                )
                .returning(Cart.id)
            ).scalar_one()
        _lock_cart(session, cart_id, now)
        if new_token is None:
            refuse_if_checkout_open(session, cart_id)
        # Under the cart lock (fresh read) and before the reservation is touched: an unchanged
        # quantity is not an edit. Raising rolls back the version advance and expiry refresh.
        if new_token is None and _line_quantity(session, cart_id, sku) == quantity:
            raise _NoChange()
        ended = _end_reservation(session, cart_id, now)

        variant = session.execute(
            select(
                Variant.id,
                Variant.status,
                Variant.stock_on_hand,
                Variant.stock_reserved,
                Product.status.label("product_status"),
            )
            .join(Product, Product.id == Variant.product_id)
            .where(Variant.sku == sku)
        ).one_or_none()
        if variant is None:
            _fail(session, VariantNotFound(sku))
        if variant.status != "active" or variant.product_status != "active":
            _fail(session, VariantInactive(sku))

        current = session.execute(
            select(CartItem.quantity).where(CartItem.cart_id == cart_id, CartItem.variant_id == variant.id)
        ).scalar_one_or_none()
        if current is None:
            lines = session.execute(
                select(func.count()).select_from(CartItem).where(CartItem.cart_id == cart_id)
            ).scalar_one()
            if lines >= MAX_CART_LINES:
                _fail(session, CartLineLimit())

        # Advisory (rule 11): reads stock after this cart's own release; holds nothing.
        available = variant.stock_on_hand - variant.stock_reserved
        if available < quantity:
            _fail(session, InsufficientAvailableStock(sku, requested=quantity, available=available))

        if current is None:
            session.execute(
                insert(CartItem).values(
                    cart_id=cart_id, variant_id=variant.id, quantity=quantity, created_at=now, updated_at=now
                )
            )
        else:
            session.execute(
                update(CartItem)
                .where(CartItem.cart_id == cart_id, CartItem.variant_id == variant.id)
                .values(quantity=quantity, updated_at=now)
                .execution_options(synchronize_session=False)
            )
        view = _cart_view(session, cart_id)
    return CartChange(cart=view, ended_reservation=ended, new_token=new_token)


def remove_line(session: Session, cart_id: int, sku: str, *, clock: Clock) -> CartChange:
    """Remove a line, even one whose variant or product has been archived.

    Removing a line that isn't in the cart (or an unknown SKU) is an idempotent no-op: the
    version advance is rolled back, nothing is released and no movement is written. Otherwise,
    in one transaction: lock the cart, end and release its active reservation, delete the line.
    Other lines, available or not, are never touched.
    """
    _sweep_for(session, sku, cart_id, clock)
    try:
        with unit_of_work(session):
            now = clock.now()
            _lock_cart(session, cart_id, now)
            refuse_if_checkout_open(session, cart_id)
            variant_id = _variant_id(session, sku)
            present = variant_id is not None and (
                session.execute(
                    select(CartItem.quantity).where(CartItem.cart_id == cart_id, CartItem.variant_id == variant_id)
                ).scalar_one_or_none()
                is not None
            )
            if not present:
                raise _NoChange()
            ended = _end_reservation(session, cart_id, now)
            session.execute(delete(CartItem).where(CartItem.cart_id == cart_id, CartItem.variant_id == variant_id))
            view = _cart_view(session, cart_id)
        return CartChange(cart=view, ended_reservation=ended)
    except _NoChange:
        pass
    with unit_of_work(session):
        return CartChange(cart=_cart_view(session, cart_id), ended_reservation=None)
