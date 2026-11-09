"""The canonical order snapshot and version hash (ADR-008, ADR-025)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.domain.orders.snapshot import SnapshotLine, canonical_snapshot, version_hash

LINES = [SnapshotLine("SOCK-M", 2, 1600, 3200), SnapshotLine("SHELL-M", 1, 12900, 12900)]
BASE: dict[str, Any] = {
    "public_ref": "0" * 32,
    "currency": "USD",
    "ship_country": "FR",
    "lines": LINES,
    "subtotal_minor": 16100,
    "shipping_minor": 1500,
    "total_minor": 17600,
}


def test_canonical_form_is_exact() -> None:
    assert canonical_snapshot(**BASE) == (
        '{"currency":"USD","lines":['
        '{"line_total_minor":12900,"quantity":1,"sku":"SHELL-M","unit_price_minor":12900},'
        '{"line_total_minor":3200,"quantity":2,"sku":"SOCK-M","unit_price_minor":1600}],'
        '"ref":"00000000000000000000000000000000","ship_country":"FR",'
        '"shipping_minor":1500,"subtotal_minor":16100,"total_minor":17600,"v":1}'
    )


def test_hash_is_sha256_of_the_canonical_form() -> None:
    expected = hashlib.sha256(canonical_snapshot(**BASE).encode("ascii")).hexdigest()
    assert version_hash(**BASE) == expected
    assert len(expected) == 64


def test_line_order_does_not_matter() -> None:
    assert version_hash(**{**BASE, "lines": list(reversed(LINES))}) == version_hash(**BASE)


@pytest.mark.parametrize(
    "change",
    [
        {"public_ref": "1" * 32},
        {"ship_country": "DE"},
        {"shipping_minor": 1501, "total_minor": 17601},
        {"lines": [SnapshotLine("SOCK-M", 3, 1600, 4800), LINES[1]]},
        {"lines": [SnapshotLine("SOCK-M", 2, 1601, 3202), LINES[1]]},
    ],
)
def test_every_approved_value_changes_the_hash(change: dict[str, Any]) -> None:
    assert version_hash(**{**BASE, **change}) != version_hash(**BASE)


@pytest.mark.parametrize("bad", [True, 1.5, "100", None])
def test_amounts_must_be_integers(bad: Any) -> None:
    with pytest.raises(TypeError):
        version_hash(**{**BASE, "subtotal_minor": bad})
    with pytest.raises(TypeError):
        version_hash(**{**BASE, "lines": [SnapshotLine("X", 1, bad, 1)]})


def test_empty_or_duplicate_lines_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one line"):
        version_hash(**{**BASE, "lines": []})
    with pytest.raises(ValueError, match="each SKU once"):
        version_hash(**{**BASE, "lines": [LINES[0], LINES[0]]})
