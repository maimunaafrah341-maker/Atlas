"""Stock levels and audited stock changes (ARCHITECTURE §3, ADR-009 groundwork).

Invariant: available = stock_on_hand - stock_reserved, and it is never negative. Changes use
a conditional UPDATE: the row changes only if the invariant still holds afterwards. That is
safe under concurrency on both SQLite and PostgreSQL, and the database CHECK constraint is
the second line of defence. Every change writes an InventoryMovement in the same
transaction. Callers own the transaction (commit or roll back).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import utcnow
from app.domain.catalog.models import Variant
from app.domain.catalog.vocab import StockStatus
from app.domain.inventory.models import MOVEMENT_REASONS, InventoryMovement
from app.errors import AtlasError


class VariantNotFound(AtlasError):
    status_code = 404
    code = "variant_not_found"

    def __init__(self, sku: str) -> None:
        super().__init__(f"No variant with SKU {sku}.", details=[{"sku": sku}])


class VariantInactive(AtlasError):
    status_code = 409
    code = "variant_inactive"

    def __init__(self, sku: str) -> None:
        super().__init__(f"Variant {sku} is not active.", details=[{"sku": sku}])


class InvalidQuantity(AtlasError):
    status_code = 422
    code = "invalid_quantity"


class InvalidMovementReason(AtlasError):
    status_code = 422
    code = "invalid_movement_reason"


class InsufficientAvailableStock(AtlasError):
    status_code = 409
    code = "insufficient_available_stock"

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(
            f"Variant {sku} has {available} available; {requested} requested.",
            details=[{"sku": sku, "requested": requested, "available": available}],
        )
        self.sku = sku
        self.requested = requested
        self.available = available


def is_low_stock(available: int, threshold: int) -> bool:
    """D-6: low when available <= threshold. A threshold of 0 disables low-stock alerts."""
    return threshold > 0 and available <= threshold


def stock_status(available: int, threshold: int) -> StockStatus:
    if available <= 0:
        return "out_of_stock"
    if is_low_stock(available, threshold):
        return "low_stock"
    return "in_stock"


def _validate_change(delta: Any, reason: str) -> int:
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise InvalidQuantity("Stock changes must be whole numbers.")
    if delta == 0:
        raise InvalidQuantity("A stock change of zero is not allowed.")
    if reason not in MOVEMENT_REASONS:
        raise InvalidMovementReason(f"Unknown movement reason {reason!r}.")
    if reason in {"initial_stock", "restock"} and delta < 0:
        raise InvalidQuantity(f"A {reason} movement must add stock.")
    return delta


def adjust_stock(
    session: Session,
    sku: str,
    delta: int,
    reason: str,
    *,
    ref: str | None = None,
    note: str | None = None,
) -> InventoryMovement:
    """Change stock_on_hand by ``delta`` and record the movement. Does not commit."""
    delta = _validate_change(delta, reason)
    variant = session.execute(select(Variant).where(Variant.sku == sku)).scalar_one_or_none()
    if variant is None:
        raise VariantNotFound(sku)
    if variant.status != "active":
        raise VariantInactive(sku)

    statement = (
        update(Variant)
        .where(
            Variant.id == variant.id,
            Variant.status == "active",
            Variant.stock_on_hand + delta >= Variant.stock_reserved,
        )
        .values(stock_on_hand=Variant.stock_on_hand + delta, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    try:
        changed = session.execute(statement).rowcount  # type: ignore[attr-defined]
    except IntegrityError:
        # Only reachable if the CHECK constraint fires; the WHERE clause should prevent it.
        # The caller owns the transaction and must roll it back.
        raise InsufficientAvailableStock(sku, requested=-delta, available=0) from None

    session.refresh(variant)
    if changed != 1:
        if variant.status != "active":
            raise VariantInactive(sku)
        raise InsufficientAvailableStock(sku, requested=-delta, available=variant.available)

    movement = InventoryMovement(
        variant_id=variant.id,
        delta_on_hand=delta,
        delta_reserved=0,
        on_hand_after=variant.stock_on_hand,
        reserved_after=variant.stock_reserved,
        reason=reason,
        ref=ref,
        note=note,
    )
    session.add(movement)
    session.flush()
    return movement


def movements_for(session: Session, sku: str) -> list[InventoryMovement]:
    """The audit trail for one variant, oldest first."""
    return list(
        session.execute(
            select(InventoryMovement)
            .join(Variant, Variant.id == InventoryMovement.variant_id)
            .where(Variant.sku == sku)
            .order_by(InventoryMovement.created_at, InventoryMovement.id)
        ).scalars()
    )
