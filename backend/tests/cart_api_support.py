"""Shared builders and checks for the /api/cart tests (test_cart_api*.py)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.cart.models import Cart
from app.domain.inventory.models import InventoryMovement
from tests.factories import V, make_product

ORIGIN = "http://testserver"
# A stock count no response or log may ever contain (D-5).
DISTINCTIVE_STOCK = 7357

FORBIDDEN_KEYS = {
    "cart_id",
    "version",
    "cart_version",
    "variant_id",
    "product_id",
    "token",
    "token_hash",
    "stock_on_hand",
    "stock_reserved",
    "on_hand",
    "reserved",
    "available_quantity",
    "quantity_available",
}


def build_shop(session: Session) -> None:
    """A small catalogue with exact stock, prices and translations."""
    make_product(
        session,
        "rain-shell",
        translations={
            "en": ("Rain Shell", "A waterproof shell."),
            "es": ("Chaqueta impermeable", "Una chaqueta impermeable."),
        },
        variants=[
            V(sku="SHELL-M", size="M", colour="navy", price_minor=12900, on_hand=DISTINCTIVE_STOCK),
            V(sku="SHELL-L", size="L", colour="navy", price_minor=13900, on_hand=3),
            V(sku="SHELL-XL", size="XL", colour="navy", price_minor=13900, on_hand=0),
        ],
    )
    make_product(
        session,
        "trail-sock",
        category="socks",
        translations={"en": ("Trail Sock", "A merino sock.")},
        variants=[V(sku="SOCK-M", size="M", colour="grey", price_minor=1600, on_hand=50)],
    )
    session.commit()


def cart_count(session: Session) -> int:
    session.expire_all()
    return session.execute(select(func.count()).select_from(Cart)).scalar_one()


def movements(session: Session, reason: str) -> int:
    session.expire_all()
    return session.execute(
        select(func.count()).select_from(InventoryMovement).where(InventoryMovement.reason == reason)
    ).scalar_one()


def set_cookie_headers(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def cookie_attributes(header: str) -> tuple[str, str, dict[str, str]]:
    """(name, value, {lower-cased attribute: value}) of one Set-Cookie header."""
    first, *rest = [part.strip() for part in header.split(";")]
    name, _, value = first.partition("=")
    attributes: dict[str, str] = {}
    for part in rest:
        key, _, val = part.partition("=")
        attributes[key.lower()] = val
    return name, value, attributes


def _keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def assert_no_leaks(response: httpx.Response, *secrets: str) -> None:
    """No internal identifiers, versions, stock counts or tokens in a response body or headers."""
    body = response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        leaked = set(_keys(response.json())) & FORBIDDEN_KEYS
        assert not leaked, f"internal fields in response: {leaked}"
    assert str(DISTINCTIVE_STOCK) not in body, "a stock count leaked"
    other_headers = "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items() if k.lower() != "set-cookie")
    for secret in secrets:
        if not secret:
            continue
        digest = hashlib.sha256(secret.encode()).hexdigest()
        for value in (secret, digest):
            assert value not in body, "token or its digest in a response body"
            assert value not in other_headers, "token or its digest in a response header"
