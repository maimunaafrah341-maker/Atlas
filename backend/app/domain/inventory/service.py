"""Stock levels and audited stock changes (ARCHITECTURE §3 and §3.1, ADR-024).

Invariant: available = stock_on_hand - stock_reserved, and it is never negative. Changes use
a conditional UPDATE: the row changes only if the invariant still holds afterwards. That is
safe under concurrency on both SQLite and PostgreSQL, and the database CHECK constraint is
the second line of defence. Every change writes an InventoryMovement in the same
transaction. Callers own the transaction (commit or roll back).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, NoReturn

from sqlalchemy import exists, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import mark_rollback_only, utcnow
from app.domain.catalog.models import Product, Variant
from app.domain.catalog.vocab import StockStatus
from app.domain.inventory.models import RESERVATION_REASONS, STOCK_ADJUSTMENT_REASONS, InventoryMovement
from app.domain.reservations.models import Reservation, ReservationItem
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
    """Not enough available stock.

    D-5: the client-facing message and details never contain the available count. Internal
    callers can still read ``self.available``.
    """

    status_code = 409
    code = "insufficient_available_stock"

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(
            f"Not enough stock of {sku} for the requested quantity.",
            details=[{"sku": sku, "requested": requested}],
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
    if reason not in STOCK_ADJUSTMENT_REASONS:
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


# --- Reservation holds and releases (M2, ADR-024, ARCHITECTURE §3.1) ------------------------
#
# These primitives move only stock_reserved. They read the quantities from reservation_items,
# so what is held, and later released, is always exactly the reservation's immutable items
# (§3.1 rule 9). Variant rows are locked in ascending id order, the last step of the global
# lock order (cart -> reservation -> variants), so concurrent holds and releases can't
# deadlock. Values are read back with RETURNING, never from ORM objects loaded earlier
# (rule 6). Neither primitive commits: the caller's transaction also contains the
# reservation's state change. Every failure marks that transaction rollback-only before it
# raises (app.db.mark_rollback_only), so even a caller that catches the error can't commit
# partial work; reservation operations run inside app.db.unit_of_work.


class StockShortfall(AtlasError):
    """One or more variants could not be held. The caller must roll back.

    D-5: no counts. ``variant_ids`` (internal) lists every variant that could not be held, so
    the reservation service can name all of them, by SKU, in one customer-facing error.
    """

    status_code = 409
    code = "insufficient_available_stock"

    def __init__(self, variant_ids: Sequence[int]) -> None:
        super().__init__("Some items are not available in the requested quantity.")
        self.variant_ids = tuple(variant_ids)


class InventoryInvariantViolation(AtlasError):
    """A hold or release was asked to do something the reservation rules forbid.

    Reaching this is a bug, not a customer error: it is logged with a reference and the client
    gets a generic 500. The caller must roll back.
    """

    status_code = 500
    code = "inventory_invariant_violation"


def _fail(session: Session, error: AtlasError) -> NoReturn:
    """Mark the transaction rollback-only, then raise: partial holds or releases can't be committed."""
    mark_rollback_only(session, f"{error.code}: {error.message}")
    raise error


def _reservation_items(session: Session, reservation_id: int) -> list[tuple[int, int]]:
    rows = session.execute(
        select(ReservationItem.variant_id, ReservationItem.quantity)
        .where(ReservationItem.reservation_id == reservation_id)
        .order_by(ReservationItem.variant_id)
    ).all()
    if not rows:
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} has no items."))
    return [(variant_id, quantity) for variant_id, quantity in rows]


def _reservation_status(session: Session, reservation_id: int) -> str:
    status = session.execute(select(Reservation.status).where(Reservation.id == reservation_id)).scalar_one_or_none()
    if status is None:
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} does not exist."))
    return status


def _has_movements(session: Session, reservation_id: int, reasons: Sequence[str]) -> bool:
    return bool(
        session.execute(
            select(
                exists().where(
                    InventoryMovement.reservation_id == reservation_id,
                    InventoryMovement.reason.in_(reasons),
                )
            )
        ).scalar_one()
    )


def hold_reservation_stock(session: Session, reservation_id: int, *, now: datetime) -> None:
    """Reserve stock for every item of an active, not-yet-held reservation. Does not commit.

    Each variant is held with one conditional UPDATE, in ascending variant id order. A hold
    needs an active variant of an active product and enough available stock. Every item is
    attempted, so a shortfall names all the variants that could not be held; the movements are
    written only when every hold succeeded. A StockShortfall marks the transaction
    rollback-only, so the holds that did succeed can only be rolled back, never committed.

    Accepted limitation (ADR-024): the product row is not locked. A hold that waits for a
    variant lock re-checks the variant row, not the product, so a hold running at the same
    moment as a product is archived can still succeed. Products are archived only by the seed
    or by hand, and releases ignore status, so such a hold is always released normally.
    """
    if _reservation_status(session, reservation_id) != "active":
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} is not active."))
    if _has_movements(session, reservation_id, RESERVATION_REASONS):
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} already has stock movements."))

    product_is_active = exists().where(Product.id == Variant.product_id, Product.status == "active")
    short: list[int] = []
    movements: list[dict[str, Any]] = []
    for variant_id, quantity in _reservation_items(session, reservation_id):
        level = session.execute(
            update(Variant)
            .where(
                Variant.id == variant_id,
                Variant.status == "active",
                product_is_active,
                Variant.stock_on_hand - Variant.stock_reserved >= quantity,
            )
            .values(stock_reserved=Variant.stock_reserved + quantity, updated_at=now)
            .returning(Variant.stock_on_hand, Variant.stock_reserved)
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if level is None:
            short.append(variant_id)
            continue
        movements.append(
            {
                "variant_id": variant_id,
                "delta_on_hand": 0,
                "delta_reserved": quantity,
                "on_hand_after": level.stock_on_hand,
                "reserved_after": level.stock_reserved,
                "reason": "reservation_hold",
                "reservation_id": reservation_id,
                "created_at": now,
            }
        )
    if short:
        _fail(session, StockShortfall(short))
    session.execute(insert(InventoryMovement), movements)


def release_reservation_stock(session: Session, reservation_id: int, *, now: datetime) -> None:
    """Return a just-ended reservation's stock. Does not commit.

    Call only after this transaction's compare-and-set moved the reservation out of
    ``active`` (§3.1 rule 7). The reason follows the new status: ``expired`` records
    ``reservation_expiry``, ``released`` records ``reservation_release``. Releases never check
    variant or product status, so stock held on an archived variant is still returned. A
    reservation that is still active, was never held, or was already released is refused.
    """
    status = _reservation_status(session, reservation_id)
    if status == "active":
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} is still active."))
    reason = "reservation_expiry" if status == "expired" else "reservation_release"
    if not _has_movements(session, reservation_id, ("reservation_hold",)):
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} never held stock."))
    if _has_movements(session, reservation_id, ("reservation_release", "reservation_expiry")):
        _fail(session, InventoryInvariantViolation(f"Reservation {reservation_id} was already released."))

    movements: list[dict[str, Any]] = []
    for variant_id, quantity in _reservation_items(session, reservation_id):
        level = session.execute(
            update(Variant)
            .where(Variant.id == variant_id, Variant.stock_reserved >= quantity)
            .values(stock_reserved=Variant.stock_reserved - quantity, updated_at=now)
            .returning(Variant.stock_on_hand, Variant.stock_reserved)
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if level is None:
            _fail(
                session,
                InventoryInvariantViolation(
                    f"Variant {variant_id} has less reserved stock than reservation {reservation_id} holds."
                ),
            )
        movements.append(
            {
                "variant_id": variant_id,
                "delta_on_hand": 0,
                "delta_reserved": -quantity,
                "on_hand_after": level.stock_on_hand,
                "reserved_after": level.stock_reserved,
                "reason": reason,
                "reservation_id": reservation_id,
                "created_at": now,
            }
        )
    session.execute(insert(InventoryMovement), movements)
