"""PayPal webhook processing (M3.3): verify, deduplicate, reconcile against PayPal.

A webhook is a **notification, never evidence**. Its body is used for exactly three things:
its event id (deduplication), its event type (whether Atlas acts on it, and what PayPal should
eventually show), and a hint to find the Atlas order. Payment state always comes from PayPal's
API through the shared ``reconcile_for_webhook`` -> ``finalize`` path (ADR-026), which never
captures and reads the order's **stored** PayPal order id, so a hint can at most cause a
reconciliation of a real order.

``handle_webhook`` steps (no transaction or lock is held during a PayPal call):

1. Checks with no PayPal call and no database write: the body's size, the transmission headers,
   one strict JSON object with a well-formed event id (at most 64 characters, never truncated)
   and event type.
2. PayPal's verification API with the received bytes unchanged. Only ``FAILURE`` rejects the
   delivery; an unavailable or failed verification request leaves it retryable.
3. One short transaction: record the delivery (insert, or atomically ``deliveries + 1`` and
   ``last_received_at``), then stop if the event was already processed. Event types Atlas
   doesn't act on are recorded as ``ignored`` and events naming no Atlas order as
   ``unmatched``; both are settled.
4. ``reconcile_for_webhook`` (outside any transaction).
5. One short transaction: store the outcome. ``processed_at`` is set only when PayPal's own
   state has caught up with the event; otherwise (and whenever PayPal can't be read) the event
   stays unprocessed and the caller answers with a retryable error, so PayPal delivers it again.

Stock is never released here: only ``finalize`` (a capture record ``DECLINED`` / ``FAILED``)
or a ``VOIDED`` PayPal order can do that, exactly as for capture and reconcile. Refunds and
reversals are out of M3.3's scope: they are recorded as ``ignored``, never treated as payments.

Nothing here stores or logs the body, the resource, the transmission headers or payer data.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import unit_of_work
from app.domain.orders.models import Order
from app.domain.payments.models import PayPalWebhookEvent
from app.domain.payments.paypal import PayPalGateway, PayPalUnavailable, WebhookTransmission
from app.domain.payments.service import WebhookReconciliation, reconcile_for_webhook
from app.errors import AtlasError

logger = logging.getLogger(__name__)

MAX_EVENT_BYTES = 64 * 1024  # PayPal's events are a few KB; anything this large isn't one
COMPLETED = "PAYMENT.CAPTURE.COMPLETED"
PENDING = "PAYMENT.CAPTURE.PENDING"
DENIED = "PAYMENT.CAPTURE.DENIED"
HANDLED_EVENTS = frozenset({COMPLETED, PENDING, DENIED})
# Future work (not M3.3): recorded as ignored, never processed as payments, logged for a person.
REFUND_EVENTS = frozenset({"PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"})
IGNORED = "ignored"
UNMATCHED = "unmatched"

_EVENT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")  # the column holds 64: longer is refused, never cut
_EVENT_TYPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_PAYPAL_ORDER_ID = re.compile(r"[A-Za-z0-9-]{1,36}")
_ORDER_REF = re.compile(r"[0-9a-f]{32}")
# Transmission headers: visible ASCII, no spaces, bounded. PayPal checks their content itself.
_HEADER_LIMITS = {
    "auth_algo": 64,
    "cert_url": 512,
    "transmission_id": 128,
    "transmission_sig": 4096,
    "transmission_time": 64,
}
_HEADER_VALUE = re.compile(r"[\x21-\x7e]+")


# --- Errors (the route maps them to HTTP in Stage 4) -------------------------------------------


class WebhooksUnavailable(AtlasError):
    status_code = 503
    code = "webhooks_unavailable"

    def __init__(self) -> None:
        super().__init__("PayPal webhooks are not configured.")


class WebhookMalformed(AtlasError):
    status_code = 400
    code = "webhook_malformed"

    def __init__(self) -> None:
        super().__init__("This is not a PayPal webhook delivery Atlas can read.")


class WebhookSignatureInvalid(AtlasError):
    status_code = 400
    code = "webhook_signature_invalid"

    def __init__(self) -> None:
        super().__init__("PayPal did not verify this webhook's signature.")


class WebhookRetryLater(AtlasError):
    status_code = 503
    code = "webhook_retry_later"

    def __init__(self) -> None:
        super().__init__("This webhook can't be processed yet. Please deliver it again later.")


# --- Parsing (only the fields Atlas uses) ------------------------------------------------------


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    event_type: str
    paypal_order_id: str | None  # resource.supplementary_data.related_ids.order_id, if well formed
    order_ref: str | None  # resource.invoice_id, else resource.custom_id (the Atlas reference)
    # Whether the event named a PayPal order at all (even a malformed one): if it did, that order
    # decides the match and the Atlas reference is never used as a fallback.
    names_paypal_order: bool = False


@dataclass(frozen=True)
class WebhookResult:
    event_id: str
    event_type: str
    outcome: str
    duplicate: bool  # the event had already been processed before this delivery


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _match(pattern: re.Pattern[str], value: Any) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _not_json(constant: str) -> object:
    raise ValueError(f"{constant} is not JSON")


def parse_event(raw_event: bytes) -> WebhookEvent:
    """The event's id, type and order hints, or ``WebhookMalformed``. Nothing else is read."""
    if len(raw_event) > MAX_EVENT_BYTES:
        raise WebhookMalformed()
    try:
        data = json.loads(raw_event.decode("utf-8"), parse_constant=_not_json)
    except (UnicodeDecodeError, ValueError):
        raise WebhookMalformed() from None
    if not isinstance(data, dict):
        raise WebhookMalformed()
    event_id = _match(_EVENT_ID, data.get("id"))
    event_type = _match(_EVENT_TYPE, data.get("event_type"))
    if event_id is None or event_type is None:
        raise WebhookMalformed()  # includes an id longer than 64 characters: refused, never truncated
    resource = _dict(data.get("resource"))
    related = _dict(_dict(resource.get("supplementary_data")).get("related_ids"))
    named = related.get("order_id")
    return WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        paypal_order_id=_match(_PAYPAL_ORDER_ID, named),
        order_ref=_match(_ORDER_REF, resource.get("invoice_id")) or _match(_ORDER_REF, resource.get("custom_id")),
        names_paypal_order=named is not None,
    )


def check_transmission(transmission: WebhookTransmission) -> None:
    for name, limit in _HEADER_LIMITS.items():
        value = getattr(transmission, name)
        if not isinstance(value, str) or len(value) > limit or not _HEADER_VALUE.fullmatch(value):
            raise WebhookMalformed()
    if not transmission.cert_url.startswith("https://"):
        raise WebhookMalformed()


# --- Processing ----------------------------------------------------------------------------------


def handle_webhook(
    session: Session,
    raw_event: bytes,
    transmission: WebhookTransmission,
    *,
    gateway: PayPalGateway,
    webhook_id: str,
    merchant_id: str,
    clock: Clock,
) -> WebhookResult:
    """Verify one delivery, record it once, and reconcile its order against PayPal.

    Raises ``WebhooksUnavailable`` (not configured), ``WebhookMalformed``,
    ``WebhookSignatureInvalid`` (PayPal answered ``FAILURE``) or ``WebhookRetryLater`` (PayPal
    unavailable, or its state hasn't caught up with the event; the event stays unprocessed).
    """
    if not webhook_id:
        raise WebhooksUnavailable()
    check_transmission(transmission)
    event = parse_event(raw_event)
    log = {"event_id": event.event_id, "event_type": event.event_type}

    try:
        verified = gateway.verify_webhook_signature(transmission, webhook_id=webhook_id, raw_event=raw_event)
    except PayPalUnavailable as error:
        logger.warning("paypal webhook verification unavailable", extra={**log, "paypal_debug_id": error.debug_id})
        raise WebhookRetryLater() from None
    except ValueError:
        raise WebhookMalformed() from None
    if not verified:
        logger.warning("paypal webhook signature rejected", extra=log)
        raise WebhookSignatureInvalid()

    with unit_of_work(session):
        now = clock.now()
        row_id, processed_at, stored_outcome = _record_delivery(session, event, now)
        if processed_at is not None:  # already settled: nothing to do, and PayPal isn't asked again
            logger.info("paypal webhook duplicate", extra=log)
            return WebhookResult(event.event_id, event.event_type, stored_outcome or IGNORED, duplicate=True)
        if event.event_type not in HANDLED_EVENTS:
            _settle(session, row_id, None, IGNORED, now)
            if event.event_type in REFUND_EVENTS:
                logger.warning("paypal refund or reversal webhook not processed (out of M3.3 scope)", extra=log)
            else:
                logger.info("paypal webhook ignored", extra=log)
            return WebhookResult(event.event_id, event.event_type, IGNORED, duplicate=False)
        matched = _find_order(session, event)
        if isinstance(matched, str):
            _settle(session, row_id, None, UNMATCHED, now)
            logger.warning("paypal webhook matches no order", extra={**log, "match": matched})
            return WebhookResult(event.event_id, event.event_type, UNMATCHED, duplicate=False)
        order_id, order_ref = matched
        _link(session, row_id, order_id)
    log["order_ref"] = order_ref

    try:
        seen = reconcile_for_webhook(session, order_id, gateway=gateway, merchant_id=merchant_id, clock=clock)
    except PayPalUnavailable as error:
        logger.warning("paypal webhook reconcile unavailable", extra={**log, "paypal_debug_id": error.debug_id})
        raise WebhookRetryLater() from None

    outcome = seen.result.result
    settled = caught_up(event.event_type, seen)
    with unit_of_work(session):
        now = clock.now()
        if settled:
            _settle(session, row_id, order_id, outcome, now)
        else:
            _note(session, row_id, outcome)  # processed_at stays NULL: the next delivery tries again
    if not settled:
        logger.info(
            "paypal webhook waiting for paypal",
            extra={**log, "outcome": outcome, "capture_status": seen.capture_status},
        )
        raise WebhookRetryLater()
    logger.info("paypal webhook processed", extra={**log, "outcome": outcome, "capture_status": seen.capture_status})
    return WebhookResult(event.event_id, event.event_type, outcome, duplicate=False)


def caught_up(event_type: str, seen: WebhookReconciliation) -> bool:
    """Whether PayPal's own state is at least as far along as the event says.

    Only decides **whether to wait** for a redelivery; the state applied is always PayPal's. A
    ``VOIDED`` PayPal order is final. Otherwise a capture record must exist, and for a completed
    or denied event it must no longer be ``PENDING``: whatever final status PayPal shows (even
    one contradicting the event) has been applied by ``finalize`` and is the answer.
    """
    if seen.paypal_order_status == "VOIDED":
        return True
    if seen.capture_status is None:
        return False
    if event_type == PENDING:
        return True
    return seen.capture_status != "PENDING"


def _record_delivery(session: Session, event: WebhookEvent, now: datetime) -> tuple[int, datetime | None, str | None]:
    """Insert the event, or count one more delivery of it, in a single atomic statement.

    ``INSERT ... ON CONFLICT (event_id) DO UPDATE SET deliveries = deliveries + 1``: concurrent
    deliveries of one event serialise on the unique index, so no increment is lost, on SQLite
    and PostgreSQL alike. ``last_received_at`` never moves backwards.
    """
    event_row = PayPalWebhookEvent
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert(event_row) if dialect == "postgresql" else sqlite.insert(event_row)
    values = insert.values(
        event_id=event.event_id,
        event_type=event.event_type,
        deliveries=1,
        received_at=now,
        last_received_at=now,
    )
    statement = values.on_conflict_do_update(
        index_elements=[event_row.event_id],
        set_={
            "deliveries": event_row.deliveries + 1,
            "last_received_at": case(
                (event_row.last_received_at < values.excluded.last_received_at, values.excluded.last_received_at),
                else_=event_row.last_received_at,
            ),
        },
    ).returning(event_row.id, event_row.processed_at, event_row.outcome)
    row = session.execute(statement).one()
    return row.id, row.processed_at, row.outcome


def _find_order(session: Session, event: WebhookEvent) -> tuple[int, str] | str:
    """The Atlas order the event points at as ``(id, reference)``, or why there is none.

    1. The event names a PayPal order: only that decides. No Atlas order with it (or a malformed
       id) is ``unmatched``: the Atlas reference is **not** a fallback then.
    2. It also carries an Atlas reference belonging to a *different* order: the identifiers
       conflict, so neither order is associated or reconciled.
    3. Only when it names no PayPal order is the Atlas reference used, and only for an order
       that has a PayPal order.

    Reconciliation then reads the matched order's **stored** PayPal order id, never the event's.
    """
    if event.names_paypal_order:
        if event.paypal_order_id is None:
            return "malformed_paypal_order_id"
        row = session.execute(
            select(Order.id, Order.public_ref).where(Order.paypal_order_id == event.paypal_order_id)
        ).one_or_none()
        if row is None:
            return "unknown_paypal_order_id"
        if event.order_ref is not None and event.order_ref != row.public_ref:
            other = session.execute(select(Order.id).where(Order.public_ref == event.order_ref)).one_or_none()
            if other is not None:
                return "conflicting_identifiers"
        return row.id, row.public_ref
    if event.order_ref is None:
        return "no_identifier"
    row = session.execute(
        select(Order.id, Order.public_ref).where(
            Order.public_ref == event.order_ref, Order.paypal_order_id.is_not(None)
        )
    ).one_or_none()
    if row is None:
        return "unknown_order_reference"
    return row.id, row.public_ref


def _link(session: Session, row_id: int, order_id: int) -> None:
    session.execute(
        update(PayPalWebhookEvent)
        .where(PayPalWebhookEvent.id == row_id, PayPalWebhookEvent.order_id.is_(None))
        .values(order_id=order_id)
        .execution_options(synchronize_session=False)
    )


def _note(session: Session, row_id: int, outcome: str) -> None:
    """The latest outcome of an event that isn't settled yet (audit only; still retryable)."""
    session.execute(
        update(PayPalWebhookEvent)
        .where(PayPalWebhookEvent.id == row_id, PayPalWebhookEvent.processed_at.is_(None))
        .values(outcome=outcome)
        .execution_options(synchronize_session=False)
    )


def _settle(session: Session, row_id: int, order_id: int | None, outcome: str, now: datetime) -> None:
    """Mark the event processed, once: a concurrent delivery that settled first wins."""
    values: dict[str, object] = {"outcome": outcome, "processed_at": now}
    if order_id is not None:
        values["order_id"] = order_id
    session.execute(
        update(PayPalWebhookEvent)
        .where(PayPalWebhookEvent.id == row_id, PayPalWebhookEvent.processed_at.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
