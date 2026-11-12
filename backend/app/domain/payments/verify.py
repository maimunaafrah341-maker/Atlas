"""Checking PayPal's capture record against the Atlas order (M3.2, ADR-026). Pure functions.

A capture counts for an order only if PayPal's own capture record shows all of:

- it belongs to the PayPal order stored on the Atlas order (``supplementary_data.related_ids``);
- its ``invoice_id`` is the Atlas order reference (set when the PayPal order was created);
- it was paid to the configured Sandbox merchant (``payee.merchant_id``). **Fails closed:** a
  record without a merchant id is a mismatch, never a pass;
- its currency is USD and its amount is exactly the order snapshot's total, compared as integer
  cents.

Any missing field is a mismatch. The capture *status* is judged separately by the caller:
only ``COMPLETED`` is paid.

Request bodies for creating the PayPal order are built here too, from the immutable snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.orders.service import OrderView
from app.domain.payments.paypal import CaptureRecord, from_paypal_value, to_paypal_value

ORDER_CURRENCY = "USD"
BRAND_NAME = "Atlas Demo Outfitters"
MAX_ITEM_NAME = 127
# Where PayPal sends the buyer after approving or cancelling on its own page. Required for the
# payer-action (redirect) approval that payment_source.paypal produces; the site's own bag page.
RETURN_PATH = "/bag?paypal=approved"
CANCEL_PATH = "/bag?paypal=cancelled"
_SITE_ORIGIN = re.compile(r"^https?://[^/?#@\s]+$")


@dataclass(frozen=True)
class Expected:
    paypal_order_id: str
    invoice_id: str  # the Atlas order reference
    merchant_id: str
    total_minor: int
    currency: str = ORDER_CURRENCY


def capture_problem(record: CaptureRecord, expected: Expected) -> str | None:
    """The first check the capture fails (a review reason), or None if it matches the order."""
    if record.related_order_id != expected.paypal_order_id:
        return "order_mismatch"
    if record.invoice_id != expected.invoice_id:
        return "reference_mismatch"
    if not record.payee_merchant_id or record.payee_merchant_id != expected.merchant_id:
        return "payee_mismatch"
    if record.currency != expected.currency:
        return "currency_mismatch"
    if from_paypal_value(record.amount_value) != expected.total_minor:
        return "amount_mismatch"
    return None


def order_request(order: OrderView, *, merchant_id: str, site_origin: str) -> Mapping[str, Any]:
    """The Orders v2 create body: one purchase unit whose items and breakdown are the snapshot.

    ``site_origin`` is the site's own trusted origin (scheme and host, no path), from which the
    return and cancel URLs are built; never anything the browser supplied.
    """
    if not _SITE_ORIGIN.match(site_origin):
        raise ValueError("site_origin must be a bare http(s) origin")
    if order.currency != ORDER_CURRENCY:
        raise ValueError("orders are in USD")
    items_total = sum(line.line_total_minor for line in order.lines)
    if items_total != order.subtotal_minor or order.subtotal_minor + order.shipping_minor != order.total_minor:
        raise ValueError("the order snapshot does not add up")

    def money(amount_minor: int) -> dict[str, str]:
        return {"currency_code": ORDER_CURRENCY, "value": to_paypal_value(amount_minor)}

    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": order.public_ref,
                "custom_id": order.public_ref,
                "invoice_id": order.public_ref,
                "payee": {"merchant_id": merchant_id},
                "amount": {
                    **money(order.total_minor),
                    "breakdown": {"item_total": money(order.subtotal_minor), "shipping": money(order.shipping_minor)},
                },
                "items": [
                    {
                        "name": line.name_snapshot[:MAX_ITEM_NAME],
                        "sku": line.sku,
                        "quantity": str(line.quantity),
                        "unit_amount": money(line.unit_price_minor),
                        "category": "PHYSICAL_GOODS",
                    }
                    for line in order.lines
                ],
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "brand_name": BRAND_NAME,
                    "shipping_preference": "NO_SHIPPING",
                    "user_action": "PAY_NOW",
                    "return_url": site_origin + RETURN_PATH,
                    "cancel_url": site_origin + CANCEL_PATH,
                }
            }
        },
    }
