"""The customer's cart over HTTP (M2.3; ADR-024, ARCHITECTURE §3.1 and §6).

A thin layer over the cart and reservation services, which own every transaction and lock.
The cart is identified only by the ``atlas_cart`` cookie; responses never contain the token,
internal ids, cart versions, variant ids or stock counts (D-5).

- ``GET`` reads. Its only possible write is the documented lazy expiry of an overdue
  reservation (exactly once, by compare-and-set). It never creates a cart.
- Every change requires a same-origin request (``require_same_origin``).
- ``Cache-Control: no-store`` is added to every /api/cart response by ``SafetyMiddleware``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, StrictInt
from sqlalchemy.orm import Session

from app.api.deps import SessionDep
from app.api.security import (
    CART_COOKIE,
    SameOrigin,
    SettingsDep,
    delete_cart_cookie,
    set_cart_cookie,
)
from app.clock import Clock
from app.config import Settings
from app.domain.cart.service import find_cart, read_cart, remove_line, set_quantity
from app.domain.catalog.schemas import Money
from app.domain.catalog.vocab import DEFAULT_LANGUAGE, SKU_PATTERN, Language
from app.domain.reservations.service import (
    CartNotFound,
    ReservationView,
    active_reservation,
    release_cart_reservation,
    reserve_cart,
)

router = APIRouter(prefix="/api/cart", tags=["cart"])


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


ClockDep = Annotated[Clock, Depends(get_clock)]
Sku = Annotated[str, Path(min_length=1, max_length=64, pattern=SKU_PATTERN)]


# --- Schemas ----------------------------------------------------------------------------------


class SetQuantityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Strict: true/"2" are not quantities. The 1-10 range is the cart service's rule (invalid_quantity).
    quantity: StrictInt


class CartLineOut(BaseModel):
    sku: str
    product_slug: str
    name: str
    lang: str
    fallback: bool
    machine_translated: bool
    size: str
    colour: str
    quantity: int
    unit_price: Money
    line_total: Money
    available: bool  # False once the variant or its product is archived; excluded from the subtotal


class ReservationItemOut(BaseModel):
    sku: str
    quantity: int


class ReservationOut(BaseModel):
    id: str  # the reservation's public id
    status: str
    expires_at: datetime
    items: list[ReservationItemOut]


class CartOut(BaseModel):
    lines: list[CartLineOut]
    item_count: int
    subtotal: Money
    expires_at: datetime | None
    reservation: ReservationOut | None


# --- Helpers ----------------------------------------------------------------------------------


def _current_cart(session: Session, request: Request, clock: Clock) -> tuple[int | None, str | None]:
    """The cookie's unexpired cart id (or None) and the raw token (or None if no cookie)."""
    token = request.cookies.get(CART_COOKIE)
    if token is None:
        return None, None
    return find_cart(session, token, clock=clock), token


def _reservation_out(view: ReservationView | None) -> ReservationOut | None:
    if view is None:
        return None
    return ReservationOut(
        id=view.public_id,
        status=view.status,
        expires_at=view.expires_at,
        items=[ReservationItemOut(sku=item.sku, quantity=item.quantity) for item in view.items],
    )


def _empty(settings: Settings) -> CartOut:
    return CartOut(
        lines=[],
        item_count=0,
        subtotal=Money(amount_minor=0, currency=settings.store_currency),
        expires_at=None,
        reservation=None,
    )


def _respond(
    session: Session,
    response: Response,
    *,
    cart_id: int,
    token: str,
    reservation: ReservationView | None,
    lang: Language,
    clock: Clock,
    settings: Settings,
) -> CartOut:
    """Build the response from a fresh read, and renew the cookie to the cart's own expiry."""
    named = read_cart(session, cart_id, lang=lang)
    lines: list[CartLineOut] = []
    subtotal = 0
    for line in named.cart.lines:
        name = named.names[line.product_slug]
        total = line.unit_price_minor * line.quantity
        if line.available:
            subtotal += total
        lines.append(
            CartLineOut(
                sku=line.sku,
                product_slug=line.product_slug,
                name=name.name,
                lang=name.lang,
                fallback=name.fallback,
                machine_translated=name.machine_translated,
                size=line.size,
                colour=line.colour,
                quantity=line.quantity,
                unit_price=Money(amount_minor=line.unit_price_minor, currency=line.currency),
                line_total=Money(amount_minor=total, currency=line.currency),
                available=line.available,
            )
        )
    set_cart_cookie(response, token, expires_at=named.cart.expires_at, now=clock.now(), settings=settings)
    return CartOut(
        lines=lines,
        item_count=sum(line.quantity for line in lines),
        subtotal=Money(amount_minor=subtotal, currency=settings.store_currency),
        expires_at=named.cart.expires_at,
        reservation=_reservation_out(reservation),
    )


# --- Endpoints --------------------------------------------------------------------------------


@router.get("", response_model=CartOut)
def get_cart(
    session: SessionDep,
    request: Request,
    response: Response,
    clock: ClockDep,
    settings: SettingsDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> CartOut:
    """The current cart. Never creates one; a stale cookie is removed."""
    cart_id, token = _current_cart(session, request, clock)
    if cart_id is None or token is None:
        if token is not None:
            delete_cart_cookie(response, settings)
        return _empty(settings)
    # Lazy expiry (ADR-024 D6) happens here, before the cart is read, so the response is consistent.
    reservation = active_reservation(session, cart_id, clock=clock)
    return _respond(
        session,
        response,
        cart_id=cart_id,
        token=token,
        reservation=reservation,
        lang=lang,
        clock=clock,
        settings=settings,
    )


@router.put("/items/{sku}", response_model=CartOut, dependencies=[SameOrigin])
def put_item(
    sku: Sku,
    body: SetQuantityIn,
    session: SessionDep,
    request: Request,
    response: Response,
    clock: ClockDep,
    settings: SettingsDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> CartOut:
    """Set a line's quantity (1-10). The first add creates the cart and sets the cookie."""
    cart_id, token = _current_cart(session, request, clock)
    change = set_quantity(session, cart_id, sku, body.quantity, clock=clock)
    token = change.new_token or token
    if token is None:  # unreachable: an existing cart was found by its token, or a new one was created
        raise RuntimeError("cart without a token")
    reservation = active_reservation(session, change.cart.cart_id, clock=clock)
    return _respond(
        session,
        response,
        cart_id=change.cart.cart_id,
        token=token,
        reservation=reservation,
        lang=lang,
        clock=clock,
        settings=settings,
    )


@router.delete("/items/{sku}", response_model=CartOut, dependencies=[SameOrigin])
def delete_item(
    sku: Sku,
    session: SessionDep,
    request: Request,
    response: Response,
    clock: ClockDep,
    settings: SettingsDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> CartOut:
    """Remove a line. Removing a line that isn't there succeeds and changes nothing."""
    cart_id, token = _current_cart(session, request, clock)
    if cart_id is None or token is None:
        if token is not None:
            delete_cart_cookie(response, settings)
        return _empty(settings)
    remove_line(session, cart_id, sku, clock=clock)
    reservation = active_reservation(session, cart_id, clock=clock)
    return _respond(
        session,
        response,
        cart_id=cart_id,
        token=token,
        reservation=reservation,
        lang=lang,
        clock=clock,
        settings=settings,
    )


@router.post("/reservation", response_model=CartOut, dependencies=[SameOrigin])
def post_reservation(
    session: SessionDep,
    request: Request,
    response: Response,
    clock: ClockDep,
    settings: SettingsDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> CartOut:
    """Hold the cart's stock for the reservation TTL. Idempotent: repeating it for the same cart
    returns the existing reservation (200) unchanged, never extended; a new one is 201.
    """
    cart_id, token = _current_cart(session, request, clock)
    if cart_id is None or token is None:
        raise CartNotFound()
    result = reserve_cart(session, cart_id, clock=clock, ttl=settings.reservation_ttl)
    response.status_code = 201 if result.created else 200
    return _respond(
        session,
        response,
        cart_id=cart_id,
        token=token,
        reservation=result.reservation,
        lang=lang,
        clock=clock,
        settings=settings,
    )


@router.delete("/reservation", response_model=CartOut, dependencies=[SameOrigin])
def delete_reservation(
    session: SessionDep,
    request: Request,
    response: Response,
    clock: ClockDep,
    settings: SettingsDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> CartOut:
    """Release the cart's reservation. Releasing with nothing active succeeds and changes nothing."""
    cart_id, token = _current_cart(session, request, clock)
    if cart_id is None or token is None:
        if token is not None:
            delete_cart_cookie(response, settings)
        return _empty(settings)
    release_cart_reservation(session, cart_id, clock=clock)
    reservation = active_reservation(session, cart_id, clock=clock)
    return _respond(
        session,
        response,
        cart_id=cart_id,
        token=token,
        reservation=reservation,
        lang=lang,
        clock=clock,
        settings=settings,
    )
