"""Cross-table inventory invariants the schema cannot check (ARCHITECTURE §3.1 rule 10).

Assumes every reservation movement was written by the reservation services: tests that use
this must not set ``stock_reserved`` directly (the factories default it to 0).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.catalog.models import Variant
from app.domain.inventory.models import RESERVATION_REASONS, InventoryMovement
from app.domain.reservations.models import Reservation, ReservationItem

END_REASON = {"released": "reservation_release", "expired": "reservation_expiry"}


def assert_inventory_invariants(session: Session) -> None:
    session.expire_all()

    # 1. stock_reserved equals the items of active reservations.
    reserved = dict(session.execute(select(Variant.id, Variant.stock_reserved)).all())
    held = dict(
        session.execute(
            select(ReservationItem.variant_id, func.sum(ReservationItem.quantity))
            .join(Reservation, Reservation.id == ReservationItem.reservation_id)
            .where(Reservation.status == "active")
            .group_by(ReservationItem.variant_id)
        ).all()
    )
    assert {vid: qty for vid, qty in reserved.items() if qty} == held, "stock_reserved != active reservation items"

    # 2. The reservation movements of each variant add up to its stock_reserved.
    moved = dict(
        session.execute(
            select(InventoryMovement.variant_id, func.sum(InventoryMovement.delta_reserved))
            .where(InventoryMovement.reason.in_(RESERVATION_REASONS))
            .group_by(InventoryMovement.variant_id)
        ).all()
    )
    for variant_id, quantity in reserved.items():
        assert moved.get(variant_id, 0) == quantity, f"variant {variant_id}: movements disagree with stock_reserved"

    # 3. Each reservation: one hold per item; ended ones released exactly once, with the right reason.
    movements: dict[int, list[tuple[int, str, int]]] = defaultdict(list)
    for reservation_id, variant_id, reason, delta in session.execute(
        select(
            InventoryMovement.reservation_id,
            InventoryMovement.variant_id,
            InventoryMovement.reason,
            InventoryMovement.delta_reserved,
        ).where(InventoryMovement.reservation_id.is_not(None))
    ):
        assert reservation_id is not None
        movements[reservation_id].append((variant_id, reason, delta))
    items: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for reservation_id, variant_id, quantity in session.execute(
        select(ReservationItem.reservation_id, ReservationItem.variant_id, ReservationItem.quantity)
    ):
        items[reservation_id].add((variant_id, quantity))
    for reservation_id, status in session.execute(select(Reservation.id, Reservation.status)):
        trail = movements.get(reservation_id, [])
        holds = sorted((vid, delta) for vid, reason, delta in trail if reason == "reservation_hold")
        ends = sorted((vid, -delta) for vid, reason, delta in trail if reason != "reservation_hold")
        assert holds == sorted(items[reservation_id]), f"reservation {reservation_id}: holds != items"
        if status == "active":
            assert ends == [], f"active reservation {reservation_id} has release movements"
        else:
            assert ends == holds, f"reservation {reservation_id}: not released exactly once"
            reasons = {reason for _vid, reason, _delta in trail if reason != "reservation_hold"}
            assert reasons == {END_REASON[status]}, f"reservation {reservation_id}: wrong release reason"
