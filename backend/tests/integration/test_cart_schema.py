"""M2.1 schema: carts, cart items, reservations, reservation items and reservation movements.

Runs on SQLite locally and on PostgreSQL in CI (TEST_DATABASE_URL). Every rule tested here
is enforced by the database itself, so it holds even if a later service has a bug.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.domain.cart.models import CART_IDLE_LIFETIME, MAX_QUANTITY_PER_LINE, Cart, CartItem
from app.domain.catalog.models import Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.reservations.models import Reservation, ReservationItem
from tests.factories import V, make_product

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))
TTL = timedelta(minutes=15)


def token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def new_cart(seed: str = "cart-1", **overrides: object) -> Cart:
    values: dict[str, object] = {
        "token_hash": token_hash(seed),
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": NOW + CART_IDLE_LIFETIME,
    }
    values.update(overrides)
    return Cart(**values)


def new_reservation(cart: Cart, seed: str = "r1", **overrides: object) -> Reservation:
    values: dict[str, object] = {
        "public_id": uuid.uuid5(uuid.NAMESPACE_URL, seed).hex,
        "cart_id": cart.id,
        "cart_version": 1,
        "status": "active",
        "created_at": NOW,
        "expires_at": NOW + TTL,
        "ended_at": None,
    }
    values.update(overrides)
    return Reservation(**values)


@pytest.fixture
def variant(db_session: Session) -> Variant:
    make_product(db_session, "cart-jacket", variants=[V(sku="CART-M", on_hand=10)])
    db_session.commit()
    found: Variant = db_session.execute(select(Variant).where(Variant.sku == "CART-M")).scalar_one()
    return found


@pytest.fixture
def cart(db_session: Session) -> Cart:
    created = new_cart()
    db_session.add(created)
    db_session.commit()
    return created


@pytest.fixture
def reservation(db_session: Session, cart: Cart) -> Reservation:
    created = new_reservation(cart)
    db_session.add(created)
    db_session.commit()
    return created


def rejects(session: Session, change: Callable[[Session], object], error: type[Exception] = IntegrityError) -> None:
    def attempt() -> None:
        change(session)
        session.flush()

    with pytest.raises(error):
        attempt()
    session.rollback()


def item(cart: Cart, variant: Variant, quantity: int) -> CartItem:
    return CartItem(cart_id=cart.id, variant_id=variant.id, quantity=quantity, created_at=NOW, updated_at=NOW)


# --- carts and cart items ----------------------------------------------------------------


def test_cart_version_defaults_to_zero(db_session: Session, cart: Cart) -> None:
    db_session.expire_all()
    assert db_session.get(Cart, cart.id).version == 0  # type: ignore[union-attr]
    # The database default applies too, not just the ORM default.
    db_session.execute(
        text(
            "INSERT INTO carts (token_hash, created_at, updated_at, expires_at) "
            "VALUES (:token_hash, :now, :now, :later)"
        ),
        {"token_hash": token_hash("raw"), "now": NOW, "later": NOW + CART_IDLE_LIFETIME},
    )
    raw_version = db_session.execute(select(Cart.version).where(Cart.token_hash == token_hash("raw"))).scalar_one()
    assert raw_version == 0


def test_cart_version_can_advance(db_session: Session, cart: Cart) -> None:
    cart.version = 7
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Cart, cart.id).version == 7  # type: ignore[union-attr]


def test_cart_line_quantity_limits(db_session: Session, cart: Cart, variant: Variant) -> None:
    db_session.add(item(cart, variant, MAX_QUANTITY_PER_LINE))
    db_session.commit()
    db_session.execute(delete(CartItem))
    db_session.commit()
    for quantity in (0, -1, MAX_QUANTITY_PER_LINE + 1):
        rejects(db_session, lambda s, q=quantity: s.add(item(cart, variant, q)))


def test_one_line_per_variant(db_session: Session, cart: Cart, variant: Variant) -> None:
    db_session.add(item(cart, variant, 1))
    db_session.commit()
    rejects(db_session, lambda s: s.add(item(cart, variant, 2)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": -1},
        {"token_hash": "short"},
        {"expires_at": NOW},  # must be after the last mutation
    ],
)
def test_invalid_carts_rejected(db_session: Session, overrides: dict[str, object]) -> None:
    rejects(db_session, lambda s: s.add(new_cart("bad", **overrides)))


def test_cart_token_hash_is_unique(db_session: Session, cart: Cart) -> None:
    rejects(db_session, lambda s: s.add(new_cart("cart-1")))


def test_deleting_a_cart_deletes_its_lines(db_session: Session, cart: Cart, variant: Variant) -> None:
    db_session.add(item(cart, variant, 2))
    db_session.commit()
    db_session.execute(delete(Cart).where(Cart.id == cart.id))
    db_session.commit()
    assert db_session.query(CartItem).count() == 0


def test_variant_in_a_cart_cannot_be_deleted(db_session: Session, cart: Cart, variant: Variant) -> None:
    db_session.add(item(cart, variant, 1))
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Variant).where(Variant.id == variant.id))
    db_session.rollback()


# --- reservations --------------------------------------------------------------------------


def test_reservation_records_its_cart_version(db_session: Session, cart: Cart) -> None:
    db_session.add(new_reservation(cart, cart_version=0))
    db_session.commit()
    db_session.expire_all()
    assert db_session.execute(select(Reservation.cart_version)).scalar_one() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"cart_version": -1},
        {"cart_version": None},  # every reservation names the cart version it was made for
        {"status": "committed"},  # D5: M3 adds commit states with its own migration
        {"status": "pending"},
        {"public_id": "abc"},
        {"expires_at": NOW},  # must be after creation
        {"ended_at": NOW + timedelta(minutes=1)},  # active reservations have not ended
        {"status": "released"},  # ended reservations must record when
        {"status": "expired"},
    ],
)
def test_invalid_reservations_rejected(db_session: Session, cart: Cart, overrides: dict[str, object]) -> None:
    rejects(db_session, lambda s: s.add(new_reservation(cart, **overrides)))


def test_at_most_one_active_reservation_per_cart(db_session: Session, reservation: Reservation, cart: Cart) -> None:
    rejects(db_session, lambda s: s.add(new_reservation(cart, "second", cart_version=2)))


def test_ended_reservations_do_not_block_a_new_active_one(db_session: Session, cart: Cart) -> None:
    ended = NOW + timedelta(minutes=5)
    db_session.add_all(
        [
            new_reservation(cart, "old-1", cart_version=1, status="released", ended_at=ended),
            new_reservation(cart, "old-2", cart_version=2, status="expired", ended_at=ended),
            new_reservation(cart, "current", cart_version=3),
        ]
    )
    db_session.commit()
    statuses = sorted(db_session.execute(select(Reservation.status)).scalars())
    assert statuses == ["active", "expired", "released"]


def test_each_cart_may_have_its_own_active_reservation(db_session: Session, cart: Cart) -> None:
    other = new_cart("cart-2")
    db_session.add(other)
    db_session.flush()
    db_session.add_all([new_reservation(cart, "a"), new_reservation(other, "b")])
    db_session.commit()


def test_reservation_public_id_is_unique(db_session: Session, reservation: Reservation) -> None:
    other = new_cart("cart-2")
    db_session.add(other)
    db_session.commit()
    rejects(db_session, lambda s: s.add(new_reservation(other, "r1")))


def test_reservation_history_cannot_be_deleted_through_its_cart(
    db_session: Session, reservation: Reservation, cart: Cart
) -> None:
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Cart).where(Cart.id == cart.id))
    db_session.rollback()


def reservation_item(reservation: Reservation, variant: Variant, quantity: int = 1) -> ReservationItem:
    return ReservationItem(reservation_id=reservation.id, variant_id=variant.id, quantity=quantity)


def test_reservation_items_hold_only_variant_and_quantity(
    db_session: Session, reservation: Reservation, variant: Variant
) -> None:
    db_session.add(reservation_item(reservation, variant, MAX_QUANTITY_PER_LINE))
    db_session.commit()
    columns = set(ReservationItem.__table__.columns.keys())
    assert columns == {"reservation_id", "variant_id", "quantity"}  # no price snapshot (D1)


def test_reservation_items_are_restricted(db_session: Session, reservation: Reservation, variant: Variant) -> None:
    db_session.add(reservation_item(reservation, variant))
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Reservation).where(Reservation.id == reservation.id))
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Variant).where(Variant.id == variant.id))
    db_session.rollback()


def test_one_item_per_variant_per_reservation(db_session: Session, reservation: Reservation, variant: Variant) -> None:
    db_session.add(reservation_item(reservation, variant))
    db_session.commit()
    rejects(db_session, lambda s: s.add(reservation_item(reservation, variant, 2)))


@pytest.mark.parametrize("quantity", [0, -1, MAX_QUANTITY_PER_LINE + 1])
def test_invalid_reservation_item_quantities_rejected(
    db_session: Session, reservation: Reservation, variant: Variant, quantity: int
) -> None:
    rejects(db_session, lambda s: s.add(reservation_item(reservation, variant, quantity)))


# --- reservation movements (D5, ADR-024) ---------------------------------------------------


def movement(
    variant: Variant, reason: str, *, on_hand: int = 0, reserved: int = 0, reservation_id: int | None = None
) -> InventoryMovement:
    return InventoryMovement(
        variant_id=variant.id,
        delta_on_hand=on_hand,
        delta_reserved=reserved,
        on_hand_after=10,
        reserved_after=2,
        reason=reason,
        reservation_id=reservation_id,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("reason", "reserved"),
    [("reservation_hold", 2), ("reservation_release", -2), ("reservation_expiry", -2)],
)
def test_reservation_movements_name_their_reservation(
    db_session: Session, variant: Variant, reservation: Reservation, reason: str, reserved: int
) -> None:
    db_session.add(movement(variant, reason, reserved=reserved, reservation_id=reservation.id))
    db_session.commit()
    linked = db_session.execute(
        select(InventoryMovement).where(InventoryMovement.reservation_id == reservation.id)
    ).scalar_one()
    assert linked.reason == reason


@pytest.mark.parametrize("reason", ["reservation_hold", "reservation_release", "reservation_expiry"])
def test_reservation_movement_without_reservation_rejected(db_session: Session, variant: Variant, reason: str) -> None:
    rejects(db_session, lambda s: s.add(movement(variant, reason, reserved=1)))


def test_reservation_movement_with_unknown_reservation_rejected(db_session: Session, variant: Variant) -> None:
    rejects(db_session, lambda s: s.add(movement(variant, "reservation_hold", reserved=1, reservation_id=999_999)))


@pytest.mark.parametrize(("reason", "on_hand"), [("initial_stock", 5), ("restock", 5), ("adjustment", -1)])
def test_stock_adjustment_with_reservation_rejected(
    db_session: Session, variant: Variant, reservation: Reservation, reason: str, on_hand: int
) -> None:
    rejects(db_session, lambda s: s.add(movement(variant, reason, on_hand=on_hand, reservation_id=reservation.id)))


@pytest.mark.parametrize(("reason", "on_hand"), [("initial_stock", 5), ("restock", 5), ("adjustment", -1)])
def test_stock_adjustment_without_reservation_accepted(
    db_session: Session, variant: Variant, reason: str, on_hand: int
) -> None:
    db_session.add(movement(variant, reason, on_hand=on_hand))
    db_session.commit()


@pytest.mark.parametrize(
    ("reason", "on_hand", "reserved"),
    [
        ("reservation_hold", 1, 2),  # reservations never move on-hand stock
        ("reservation_release", 0, 0),
        ("restock", 5, 2),  # stock adjustments never move reserved stock
        ("adjustment", -1, 1),
        ("sale", -1, -1),  # D5: sale arrives with M3
        ("reservation_commit", 0, -1),
    ],
)
def test_movement_reason_and_deltas_must_agree(
    db_session: Session, variant: Variant, reservation: Reservation, reason: str, on_hand: int, reserved: int
) -> None:
    linked = reservation.id if reason.startswith("reservation_") else None
    rejects(
        db_session,
        lambda s: s.add(movement(variant, reason, on_hand=on_hand, reserved=reserved, reservation_id=linked)),
    )


def test_reservation_with_movements_cannot_be_deleted(
    db_session: Session, variant: Variant, reservation: Reservation
) -> None:
    db_session.add(movement(variant, "reservation_hold", reserved=1, reservation_id=reservation.id))
    db_session.commit()
    with pytest.raises(IntegrityError):
        db_session.execute(delete(Reservation).where(Reservation.id == reservation.id))
    db_session.rollback()


# --- UTC timestamps, identical on SQLite and PostgreSQL -----------------------------------


def test_timestamps_round_trip_as_aware_utc(db_session: Session) -> None:
    created = datetime(2026, 11, 7, 14, 30, tzinfo=IST)  # 09:00 UTC
    db_session.add(new_cart("tz", created_at=created, updated_at=created, expires_at=created + CART_IDLE_LIFETIME))
    db_session.commit()
    db_session.expire_all()
    stored = db_session.execute(select(Cart).where(Cart.token_hash == token_hash("tz"))).scalar_one()
    assert stored.created_at == NOW
    assert stored.created_at.tzinfo is UTC
    assert stored.expires_at - stored.created_at == CART_IDLE_LIFETIME
    assert stored.expires_at > NOW  # aware comparisons work on both databases


def test_expiry_comparisons_run_in_sql(db_session: Session, reservation: Reservation) -> None:
    def due(at: datetime) -> list[str]:
        return list(db_session.execute(select(Reservation.public_id).where(Reservation.expires_at <= at)).scalars())

    assert due(NOW + TTL - timedelta(seconds=1)) == []
    assert len(due(NOW + TTL)) == 1
    # The same instant in another offset gives the same answer.
    assert len(due((NOW + TTL).astimezone(IST))) == 1


def test_naive_timestamps_are_refused(db_session: Session) -> None:
    naive = datetime(2026, 11, 7, 9, 0)
    rejects(
        db_session,
        lambda s: s.add(new_cart("naive", created_at=naive, updated_at=naive, expires_at=naive + CART_IDLE_LIFETIME)),
        error=StatementError,
    )
