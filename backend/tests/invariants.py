"""Cross-table inventory invariants the schema cannot check (ARCHITECTURE §3.1 rule 10, ADR-025).

Assumes every reservation movement was written by the services: tests that use this must not
set ``stock_reserved`` directly (the factories default it to 0).

1. A variant's ``stock_reserved`` equals the items of its **holding** reservations: ``active``
   (cart) and ``order_hold`` (adopted by an order).
2. A variant's ``stock_reserved`` equals the sum of its reservation-linked movements' reserved
   deltas (hold +, release / expiry / sale -).
3. Each reservation is held once per item, and ended at most once: none while holding, exactly
   one release or expiry for ``released`` / ``expired``, exactly one sale for ``committed``
   (a sale lowers on-hand and reserved stock by the same quantity).
4. Orders and their reservations agree (ADR-025), and a cart has at most one open order.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.catalog.models import Variant
from app.domain.inventory.models import RESERVATION_LINKED_REASONS, InventoryMovement
from app.domain.orders.models import OPEN_STATUSES, Order
from app.domain.reservations.models import HOLDING_STATUSES, Reservation, ReservationItem

END_REASON = {"released": "reservation_release", "expired": "reservation_expiry", "committed": "sale"}

# The reservation status each order status allows (M3.1 reaches only the first three rows' states).
ORDER_RESERVATION = {
    "pending_approval": {"order_hold"},
    "approved": {"order_hold"},
    "awaiting_payment": {"order_hold"},
    "capturing": {"order_hold"},
    "payment_pending": {"order_hold"},
    "needs_review": {"order_hold"},
    "paid": {"committed"},
    "cancelled": {"released", "expired"},
    "expired": {"expired"},
    "payment_failed": {"released"},
}


def assert_inventory_invariants(session: Session) -> None:
    session.expire_all()

    # 1. stock_reserved equals the items of holding reservations.
    reserved = dict(session.execute(select(Variant.id, Variant.stock_reserved)).all())
    held = dict(
        session.execute(
            select(ReservationItem.variant_id, func.sum(ReservationItem.quantity))
            .join(Reservation, Reservation.id == ReservationItem.reservation_id)
            .where(Reservation.status.in_(HOLDING_STATUSES))
            .group_by(ReservationItem.variant_id)
        ).all()
    )
    assert {vid: qty for vid, qty in reserved.items() if qty} == held, "stock_reserved != holding reservation items"

    # 2. The reservation-linked movements of each variant add up to its stock_reserved.
    moved = dict(
        session.execute(
            select(InventoryMovement.variant_id, func.sum(InventoryMovement.delta_reserved))
            .where(InventoryMovement.reason.in_(RESERVATION_LINKED_REASONS))
            .group_by(InventoryMovement.variant_id)
        ).all()
    )
    for variant_id, quantity in reserved.items():
        assert moved.get(variant_id, 0) == quantity, f"variant {variant_id}: movements disagree with stock_reserved"

    # 3. Each reservation: one hold per item; ended ones ended exactly once, with the right reason.
    movements: dict[int, list[tuple[int, str, int, int]]] = defaultdict(list)
    for reservation_id, variant_id, reason, delta_reserved, delta_on_hand in session.execute(
        select(
            InventoryMovement.reservation_id,
            InventoryMovement.variant_id,
            InventoryMovement.reason,
            InventoryMovement.delta_reserved,
            InventoryMovement.delta_on_hand,
        ).where(InventoryMovement.reservation_id.is_not(None))
    ):
        assert reservation_id is not None
        movements[reservation_id].append((variant_id, reason, delta_reserved, delta_on_hand))
    items: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for reservation_id, variant_id, quantity in session.execute(
        select(ReservationItem.reservation_id, ReservationItem.variant_id, ReservationItem.quantity)
    ):
        items[reservation_id].add((variant_id, quantity))
    for reservation_id, status in session.execute(select(Reservation.id, Reservation.status)):
        trail = movements.get(reservation_id, [])
        holds = sorted((vid, delta) for vid, reason, delta, _ in trail if reason == "reservation_hold")
        ends = sorted((vid, -delta) for vid, reason, delta, _ in trail if reason != "reservation_hold")
        assert holds == sorted(items[reservation_id]), f"reservation {reservation_id}: holds != items"
        if status in HOLDING_STATUSES:
            assert ends == [], f"{status} reservation {reservation_id} has ending movements"
            continue
        assert ends == holds, f"reservation {reservation_id}: not ended exactly once"
        reasons = {reason for _vid, reason, _dr, _doh in trail if reason != "reservation_hold"}
        assert reasons == {END_REASON[status]}, f"reservation {reservation_id}: wrong ending reason"
        for _vid, reason, delta_reserved, delta_on_hand in trail:
            if reason == "sale":
                assert delta_on_hand == delta_reserved < 0, f"reservation {reservation_id}: unbalanced sale"

    # 4. Orders agree with their reservations; at most one open order per cart.
    rows = session.execute(
        select(Order.cart_id, Order.status, Reservation.status.label("reservation_status")).join(
            Reservation, Reservation.id == Order.reservation_id
        )
    ).all()
    for row in rows:
        allowed = ORDER_RESERVATION[row.status]
        assert row.reservation_status in allowed, f"{row.status} order with a {row.reservation_status} reservation"
    open_per_cart = Counter(row.cart_id for row in rows if row.status in OPEN_STATUSES)
    assert all(count == 1 for count in open_per_cart.values()), "a cart has more than one open order"
    adopted = session.execute(
        select(func.count()).select_from(Reservation).where(Reservation.status.in_(("order_hold", "committed")))
    ).scalar_one()
    assert adopted == sum(row.reservation_status in ("order_hold", "committed") for row in rows), (
        "an order_hold or committed reservation has no order"
    )
