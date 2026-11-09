"""The canonical order snapshot and its SHA-256 ``version_hash`` (ADR-008, ADR-025).

The hash covers exactly what the customer approves and pays: the order reference, currency,
destination, each line's SKU, quantity, unit price and line total, and the subtotal, shipping
and total, all in integer minor units. It excludes everything presentational (product names,
language, timestamps), so changing the display language never changes it.

Canonical form: a JSON object with sorted keys, no insignificant whitespace, ASCII only, lines
sorted by SKU, and a ``v`` field so the format can change without ambiguity. Pure: no database
or clock access.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

SNAPSHOT_FORMAT = 1


@dataclass(frozen=True)
class SnapshotLine:
    sku: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int


def _whole(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer amount of minor units")
    return value


def canonical_snapshot(
    *,
    public_ref: str,
    currency: str,
    ship_country: str,
    lines: Sequence[SnapshotLine],
    subtotal_minor: int,
    shipping_minor: int,
    total_minor: int,
) -> str:
    """The canonical JSON text the hash is computed over."""
    if not lines:
        raise ValueError("an order snapshot needs at least one line")
    skus = [line.sku for line in lines]
    if len(set(skus)) != len(skus):
        raise ValueError("an order snapshot lists each SKU once")
    document = {
        "v": SNAPSHOT_FORMAT,
        "ref": public_ref,
        "currency": currency,
        "ship_country": ship_country,
        "lines": [
            {
                "sku": line.sku,
                "quantity": _whole("quantity", line.quantity),
                "unit_price_minor": _whole("unit_price_minor", line.unit_price_minor),
                "line_total_minor": _whole("line_total_minor", line.line_total_minor),
            }
            for line in sorted(lines, key=lambda line: line.sku)
        ],
        "subtotal_minor": _whole("subtotal_minor", subtotal_minor),
        "shipping_minor": _whole("shipping_minor", shipping_minor),
        "total_minor": _whole("total_minor", total_minor),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def version_hash(
    *,
    public_ref: str,
    currency: str,
    ship_country: str,
    lines: Sequence[SnapshotLine],
    subtotal_minor: int,
    shipping_minor: int,
    total_minor: int,
) -> str:
    """SHA-256 hex digest of the canonical snapshot."""
    text = canonical_snapshot(
        public_ref=public_ref,
        currency=currency,
        ship_country=ship_country,
        lines=lines,
        subtotal_minor=subtotal_minor,
        shipping_minor=shipping_minor,
        total_minor=total_minor,
    )
    return hashlib.sha256(text.encode("ascii")).hexdigest()
