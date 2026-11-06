"""Test-only builders for catalogue rows. They set stock directly; real code uses inventory.adjust_stock."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant


@dataclass
class V:
    sku: str
    size: str = "M"
    colour: str = "black"
    price_minor: int = 5000
    on_hand: int = 10
    reserved: int = 0
    threshold: int = 0
    status: str = "active"


def make_product(
    session: Session,
    slug: str,
    *,
    category: str = "jackets",
    tags: tuple[str, ...] = (),
    translations: dict[str, tuple[str, str]] | None = None,
    variants: list[V] | None = None,
    status: str = "active",
    sort_order: int = 1000,
) -> Product:
    product = Product(slug=slug, category=category, status=status, sort_order=sort_order)
    session.add(product)
    session.flush()
    for lang, (name, description) in (translations or {"en": (f"Test {slug}", "Test product.")}).items():
        translation = ProductTranslation(product_id=product.id, lang=lang, machine_translated=lang != "en")
        translation.set_text(name, description)
        session.add(translation)
    for tag in tags:
        session.add(ProductTag(product_id=product.id, tag=tag))
    for variant in variants if variants is not None else [V(sku=f"TST-{product.id}-M")]:
        session.add(
            Variant(
                product_id=product.id,
                sku=variant.sku,
                size=variant.size,
                colour=variant.colour,
                price_minor=variant.price_minor,
                currency="USD",
                stock_on_hand=variant.on_hand,
                stock_reserved=variant.reserved,
                low_stock_threshold=variant.threshold,
                status=variant.status,
            )
        )
    session.flush()
    return product
