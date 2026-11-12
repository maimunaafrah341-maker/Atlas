"""PayPal webhook receiver (M3.3, ADR-027): ``POST /api/webhooks/paypal``.

PayPal's servers call this, not a browser: there is no cart cookie and no Origin, so it
deliberately has neither the cart dependency nor the same-origin check. It is authenticated
instead by PayPal's signature verification API (inside ``handle_webhook``).

The route only does HTTP work: it refuses early when webhooks are off, bounds the body while
reading it (413 beyond ``MAX_EVENT_BYTES``, whatever Content-Length claims), takes exactly one
of each transmission header, and runs the domain service in the thread pool. Everything else,
including every PayPal call, is in ``app.domain.payments.webhooks``. It never captures.

Responses (``Cache-Control: no-store`` from ``SafetyMiddleware``, and never a cookie):
- 200: processed, duplicate, ignored or unmatched. PayPal stops redelivering.
- 400 ``webhook_malformed`` / ``webhook_signature_invalid``: not a delivery Atlas accepts.
- 413 ``webhook_too_large``.
- 503 ``webhook_retry_later`` (PayPal unavailable, or its state hasn't caught up) and
  ``webhooks_unavailable`` (not configured): retryable, so PayPal delivers again later.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.domain.payments.paypal import PayPalGateway, WebhookTransmission
from app.domain.payments.webhooks import (
    MAX_EVENT_BYTES,
    WebhookMalformed,
    WebhookResult,
    WebhooksUnavailable,
    handle_webhook,
)
from app.errors import AtlasError

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# WebhookTransmission field -> PayPal's request header (case-insensitive).
TRANSMISSION_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


class WebhookTooLarge(AtlasError):
    status_code = 413
    code = "webhook_too_large"

    def __init__(self) -> None:
        super().__init__("This webhook body is too large to be a PayPal event.")


class WebhookAck(BaseModel):
    received: Literal[True] = True
    event_type: str
    outcome: str
    duplicate: bool


def _transmission(request: Request) -> WebhookTransmission:
    values: dict[str, str] = {}
    for field, header in TRANSMISSION_HEADERS.items():
        found = request.headers.getlist(header)
        if len(found) != 1:  # missing or repeated
            raise WebhookMalformed()
        values[field] = found[0]
    return WebhookTransmission(**values)


async def _bounded_body(request: Request) -> bytes:
    declared = request.headers.getlist("content-length")
    if len(declared) > 1:
        raise WebhookMalformed()
    if declared and declared[0].isdigit() and int(declared[0]) > MAX_EVENT_BYTES:
        raise WebhookTooLarge()  # refused before reading anything
    body = bytearray()
    async for chunk in request.stream():  # a lying or absent Content-Length is still bounded
        body += chunk
        if len(body) > MAX_EVENT_BYTES:
            raise WebhookTooLarge()
    return bytes(body)


@router.post("/paypal", response_model=WebhookAck)
async def receive_paypal_webhook(request: Request) -> WebhookAck:
    settings: Settings = request.app.state.settings
    gateway: PayPalGateway | None = request.app.state.paypal
    if gateway is None or not settings.webhooks_enabled:
        raise WebhooksUnavailable()  # fail closed, before reading the body
    transmission = _transmission(request)
    raw_event = await _bounded_body(request)

    def process() -> WebhookResult:
        # The service opens its own short transactions and holds none during PayPal calls.
        with request.app.state.sessions() as session:
            return handle_webhook(
                session,
                raw_event,
                transmission,
                gateway=gateway,
                webhook_id=settings.paypal_webhook_id.strip(),
                merchant_id=settings.paypal_merchant_id.strip(),
                clock=request.app.state.clock,
            )

    result = await run_in_threadpool(process)
    return WebhookAck(event_type=result.event_type, outcome=result.outcome, duplicate=result.duplicate)
