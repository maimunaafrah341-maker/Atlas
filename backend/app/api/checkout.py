"""Orders over HTTP (M3.1; ADR-008, ADR-025) and the public checkout configuration.

Orders belong to the cart identified by the ``atlas_cart`` cookie, and every endpoint looks the
order up by its public reference **and** that cart: another cart's reference is simply
``order_not_found``. The routes live under ``/api/cart/orders`` because the cart cookie is
scoped to ``Path=/api/cart`` (ARCHITECTURE §6); that also gives them the cart's
``Cache-Control: no-store``. Changes require a same-origin request, like every cart change.

Responses never contain internal ids, cart versions, variant ids, stock counts or the token
(D-5).

Payments (M3.2, ADR-026): ``paypal-order``, ``capture`` and ``reconcile`` are POSTs with the same
ownership, same-origin and no-store protections. The browser only identifies the order and
forwards PayPal's order id; it never reports a payment status. ``GET`` stays read-only for
payments. Without a complete Sandbox configuration these three answer 503
``payments_unavailable`` and everything else keeps working.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.cart import ClockDep, _current_cart
from app.api.deps import SessionDep
from app.api.security import SameOrigin, SettingsDep
from app.db import unit_of_work
from app.domain.catalog.schemas import Money
from app.domain.catalog.service import product_names
from app.domain.catalog.vocab import DEFAULT_LANGUAGE, Language
from app.domain.orders.service import OrderNotFound, OrderView, cancel_order, create_order, get_order
from app.domain.payments.paypal import PayPalGateway
from app.domain.payments.service import (
    PaymentResult,
    PaymentsUnavailable,
    capture_payment,
    reconcile_payment,
    start_paypal_checkout,
)
from app.domain.reservations.service import CartNotFound

router = APIRouter(prefix="/api/cart/orders", tags=["orders"])
config_router = APIRouter(prefix="/api/checkout", tags=["checkout"])

OrderRef = Annotated[str, Path(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]


def get_paypal(request: Request) -> PayPalGateway:
    """The configured Sandbox gateway, or 503 payments_unavailable (the rest of the shop is unaffected)."""
    gateway: PayPalGateway | None = request.app.state.paypal
    if gateway is None:
        raise PaymentsUnavailable()
    return gateway


PayPalDep = Annotated[PayPalGateway, Depends(get_paypal)]


# --- Schemas ----------------------------------------------------------------------------------


class CreateOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated by the shipping service (invalid_country_code / unsupported_destination).
    ship_country: str = Field(min_length=1, max_length=8)


class OrderLineOut(BaseModel):
    sku: str
    product_slug: str
    name: str  # live catalogue name in the requested language; the snapshot keeps the English one
    lang: str
    fallback: bool
    machine_translated: bool
    size: str
    colour: str
    quantity: int
    unit_price: Money
    line_total: Money


class ShippingOut(BaseModel):
    country: str
    zone: str
    price: Money


class OrderOut(BaseModel):
    ref: str
    status: str
    version_hash: str  # presented back when the customer approves the order (M3.2, ADR-008)
    lines: list[OrderLineOut]
    item_count: int
    subtotal: Money
    shipping: ShippingOut
    total: Money
    hold_expires_at: datetime
    created_at: datetime


class StartPaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_hash: str = Field(pattern=r"^[0-9a-f]{64}$")  # the snapshot the customer approves (ADR-008)


class CaptureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Identifies which PayPal order the buyer approved; it must equal the stored one. Not proof of anything.
    paypal_order_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,36}$")


class StartPaymentOut(BaseModel):
    paypal_order_id: str  # returned to the JS SDK's createOrder
    order: OrderOut


class PaymentOut(BaseModel):
    # paid | pending | processing | instrument_declined | payer_action_required | failed |
    # needs_review | awaiting_payment | closed. The order's stored status is the truth.
    result: str
    # True for instrument_declined and payer_action_required: the buyer can try again in PayPal
    # (the JS SDK's actions.restart()); the stock hold is kept.
    recoverable: bool
    order: OrderOut


class CheckoutConfigOut(BaseModel):
    payments_enabled: bool
    paypal_client_id: str | None = None  # public; only when payments are enabled
    currency: str = "USD"
    intent: str = "capture"


# --- Helpers ----------------------------------------------------------------------------------


def _order_out(session: Session, view: OrderView, lang: Language) -> OrderOut:
    names = product_names(session, {line.product_slug for line in view.lines}, lang)
    currency = view.currency
    lines = []
    for line in view.lines:
        name = names.get(line.product_slug)
        lines.append(
            OrderLineOut(
                sku=line.sku,
                product_slug=line.product_slug,
                name=name.name if name else line.name_snapshot,
                lang=name.lang if name else "en",
                fallback=name.fallback if name else lang != "en",
                machine_translated=name.machine_translated if name else False,
                size=line.size,
                colour=line.colour,
                quantity=line.quantity,
                unit_price=Money(amount_minor=line.unit_price_minor, currency=currency),
                line_total=Money(amount_minor=line.line_total_minor, currency=currency),
            )
        )
    return OrderOut(
        ref=view.public_ref,
        status=view.status,
        version_hash=view.version_hash,
        lines=lines,
        item_count=sum(line.quantity for line in view.lines),
        subtotal=Money(amount_minor=view.subtotal_minor, currency=currency),
        shipping=ShippingOut(
            country=view.ship_country,
            zone=view.shipping_zone,
            price=Money(amount_minor=view.shipping_minor, currency=currency),
        ),
        total=Money(amount_minor=view.total_minor, currency=currency),
        hold_expires_at=view.hold_expires_at,
        created_at=view.created_at,
    )


def _payment_out(session: Session, result: PaymentResult, lang: Language) -> PaymentOut:
    return PaymentOut(
        result=result.result, recoverable=result.recoverable, order=_read_names(session, result.order, lang)
    )


def _owner(session: Session, request: Request, clock: ClockDep) -> int:
    cart_id, _token = _current_cart(session, request, clock)
    if cart_id is None:
        raise OrderNotFound()
    return cart_id


def _read_names(session: Session, view: OrderView, lang: Language) -> OrderOut:
    with unit_of_work(session):  # read-only: live product names in the requested language
        return _order_out(session, view, lang)


# --- Endpoints --------------------------------------------------------------------------------


@router.post("", response_model=OrderOut, dependencies=[SameOrigin])
def post_order(
    body: CreateOrderIn,
    session: SessionDep,
    request: Request,
    response: Response,
    clock: ClockDep,
    settings: SettingsDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> OrderOut:
    """Create an order from the reserved bag (201), adopting its reservation. If the cart already
    has an open order, that order is returned (200), so a timed-out request can be retried.
    """
    cart_id, _token = _current_cart(session, request, clock)
    if cart_id is None:
        raise CartNotFound()
    result = create_order(
        session, cart_id, ship_country=body.ship_country, lang=lang, clock=clock, hold=settings.order_hold
    )
    response.status_code = 201 if result.created else 200
    return _read_names(session, result.order, lang)


@router.get("/{ref}", response_model=OrderOut)
def read_order(
    ref: OrderRef,
    session: SessionDep,
    request: Request,
    clock: ClockDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> OrderOut:
    """The cart's order. A hold past its deadline is expired on read (lazy, ADR-025)."""
    cart_id, _token = _current_cart(session, request, clock)
    if cart_id is None:
        raise OrderNotFound()
    return _read_names(session, get_order(session, cart_id, ref, clock=clock), lang)


@router.post("/{ref}/cancel", response_model=OrderOut, dependencies=[SameOrigin])
def post_cancel(
    ref: OrderRef,
    session: SessionDep,
    request: Request,
    clock: ClockDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> OrderOut:
    """Cancel the order before payment and release its stock. Repeating it changes nothing."""
    cart_id, _token = _current_cart(session, request, clock)
    if cart_id is None:
        raise OrderNotFound()
    return _read_names(session, cancel_order(session, cart_id, ref, clock=clock), lang)


@router.post("/{ref}/paypal-order", response_model=StartPaymentOut, dependencies=[SameOrigin])
def post_paypal_order(
    ref: OrderRef,
    body: StartPaymentIn,
    session: SessionDep,
    request: Request,
    clock: ClockDep,
    settings: SettingsDep,
    paypal: PayPalDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> StartPaymentOut:
    """Approve the reviewed order (its version hash) and create its one PayPal order. Repeatable."""
    started = start_paypal_checkout(
        session,
        _owner(session, request, clock),
        ref,
        body.version_hash,
        gateway=paypal,
        merchant_id=settings.paypal_merchant_id.strip(),
        clock=clock,
    )
    return StartPaymentOut(paypal_order_id=started.paypal_order_id, order=_read_names(session, started.order, lang))


@router.post("/{ref}/capture", response_model=PaymentOut, dependencies=[SameOrigin])
def post_capture(
    ref: OrderRef,
    body: CaptureIn,
    session: SessionDep,
    request: Request,
    clock: ClockDep,
    settings: SettingsDep,
    paypal: PayPalDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> PaymentOut:
    """After the buyer approved in PayPal: capture on the server, verify, finalize. Repeatable."""
    result = capture_payment(
        session,
        _owner(session, request, clock),
        ref,
        body.paypal_order_id,
        gateway=paypal,
        merchant_id=settings.paypal_merchant_id.strip(),
        clock=clock,
    )
    return _payment_out(session, result, lang)


@router.post("/{ref}/reconcile", response_model=PaymentOut, dependencies=[SameOrigin])
def post_reconcile(
    ref: OrderRef,
    session: SessionDep,
    request: Request,
    clock: ClockDep,
    settings: SettingsDep,
    paypal: PayPalDep,
    lang: Language = DEFAULT_LANGUAGE,
) -> PaymentOut:
    """Ask PayPal about a payment whose outcome isn't known yet, and apply what it says."""
    result = reconcile_payment(
        session,
        _owner(session, request, clock),
        ref,
        gateway=paypal,
        merchant_id=settings.paypal_merchant_id.strip(),
        clock=clock,
    )
    return _payment_out(session, result, lang)


@config_router.get("/config", response_model=CheckoutConfigOut)
def checkout_config(request: Request, settings: SettingsDep) -> CheckoutConfigOut:
    """Whether the PayPal Sandbox payment step is available, and the public client id for the SDK."""
    enabled = request.app.state.paypal is not None
    return CheckoutConfigOut(
        payments_enabled=enabled, paypal_client_id=settings.paypal_client_id.strip() if enabled else None
    )
