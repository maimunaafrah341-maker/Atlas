"""unit_of_work and rollback-only transactions (ARCHITECTURE §3.1, ADR-024).

A failed multi-item hold must never be committed, whether its StockShortfall propagates or a
caller catches it. Runs on SQLite locally and on PostgreSQL in CI.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db import (
    RollbackOnlyTransaction,
    TransactionAlreadyOpen,
    _refuse_rollback_only_commit,
    create_db_engine,
    mark_rollback_only,
    rollback_only_reason,
    session_factory,
    unit_of_work,
)
from app.domain.cart.models import Cart
from app.domain.catalog.models import Variant
from app.domain.inventory.service import (
    InventoryInvariantViolation,
    StockShortfall,
    hold_reservation_stock,
    release_reservation_stock,
)
from app.domain.reservations.models import Reservation
from tests.factories import V, make_product
from tests.invariants import assert_inventory_invariants
from tests.reservation_builders import NOW, end, movement_count, new_cart, new_reservation, stock


class Abort(BaseException):
    """Not an Exception: unit_of_work must roll back on anything that escapes."""


@pytest.fixture
def factory(migrated_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(migrated_database_url)
    factory = session_factory(engine)
    with factory() as session:
        make_product(session, "uow", variants=[V(sku="U-S", size="S", on_hand=5), V(sku="U-M", on_hand=0)])
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session


def counts(session: Session) -> tuple[int, int, int]:
    """(carts, reservations, movements), read in a transaction that is then closed."""
    try:
        carts = session.execute(select(func.count()).select_from(Cart)).scalar_one()
        reservations = session.execute(select(func.count()).select_from(Reservation)).scalar_one()
        return carts, reservations, movement_count(session)
    finally:
        session.rollback()


def assert_untouched(session: Session) -> None:
    assert counts(session) == (0, 0, 0)
    assert stock(session, "U-S") == (5, 0)
    session.rollback()
    assert not session.in_transaction()
    assert rollback_only_reason(session) is None


def run_unit(session: Session, body: Callable[[], object]) -> None:
    with unit_of_work(session):
        body()


def short_hold(session: Session) -> None:
    """U-S can be held, U-M can't: the U-S hold succeeds before the shortfall is raised."""
    cart = new_cart(session, "c1")
    reservation_id = new_reservation(session, cart, {"U-S": 2, "U-M": 1})
    hold_reservation_stock(session, reservation_id, now=NOW)


# --- unit_of_work ------------------------------------------------------------------------


def test_commits_on_success(session: Session) -> None:
    with unit_of_work(session):
        new_cart(session, "c1")
    assert not session.in_transaction()
    assert counts(session) == (1, 0, 0)


@pytest.mark.parametrize("error", [ValueError("boom"), Abort()], ids=["exception", "base-exception"])
def test_any_escaping_exception_rolls_back(session: Session, error: BaseException) -> None:
    def body() -> None:
        new_cart(session, "c1")
        raise error

    with pytest.raises(type(error)):
        run_unit(session, body)
    assert_untouched(session)


def test_refuses_to_join_an_open_transaction(session: Session) -> None:
    session.execute(select(1))  # autobegins a transaction
    with pytest.raises(TransactionAlreadyOpen), unit_of_work(session):
        pass
    session.rollback()


def test_mark_needs_an_open_transaction(session: Session) -> None:
    with pytest.raises(RuntimeError, match="needs an open transaction"):
        mark_rollback_only(session, "nothing to mark")


# --- StockShortfall: propagated and caught -------------------------------------------------


def test_propagated_shortfall_rolls_back_every_hold(session: Session) -> None:
    with pytest.raises(StockShortfall):
        run_unit(session, lambda: short_hold(session))
    assert_untouched(session)


def test_caught_shortfall_inside_a_unit_of_work_cannot_commit(session: Session) -> None:
    def swallow() -> None:
        # The bug this protects against: swallow the error and carry on.
        with contextlib.suppress(StockShortfall):
            short_hold(session)

    with pytest.raises(RollbackOnlyTransaction, match="insufficient_available_stock"):
        run_unit(session, swallow)
    assert_untouched(session)


def test_caught_shortfall_cannot_be_committed_directly(session: Session) -> None:
    # No unit_of_work at all: the project session factory's before_commit listener refuses.
    with contextlib.suppress(StockShortfall):
        short_hold(session)
    with pytest.raises(RollbackOnlyTransaction):
        session.commit()
    assert session.in_transaction()  # the refused commit leaves the transaction to be rolled back
    session.rollback()
    assert_untouched(session)


@pytest.mark.parametrize("finish", ["rollback", "close"])
def test_rollback_only_is_cleared_and_the_session_is_reusable(session: Session, finish: str) -> None:
    try:
        short_hold(session)
    except StockShortfall:
        assert rollback_only_reason(session) is not None
    getattr(session, finish)()
    assert rollback_only_reason(session) is None

    with unit_of_work(session):  # reuse: a clean transaction commits normally
        new_cart(session, "c2")
    assert counts(session) == (1, 0, 0)


def test_a_failed_partial_release_cannot_be_committed(session: Session) -> None:
    with unit_of_work(session):
        reservation_id = new_reservation(session, new_cart(session, "c1"), {"U-S": 2})
        hold_reservation_stock(session, reservation_id, now=NOW)
    # Corrupt the counter so the release fails after its first check.
    with unit_of_work(session):
        session.execute(update(Variant).where(Variant.sku == "U-S").values(stock_reserved=1))

    def swallow() -> None:
        end(session, reservation_id, "released")
        with contextlib.suppress(InventoryInvariantViolation):
            release_reservation_stock(session, reservation_id, now=NOW)

    with pytest.raises(RollbackOnlyTransaction):
        run_unit(session, swallow)
    assert stock(session, "U-S") == (5, 1)
    status = session.execute(select(Reservation.status).where(Reservation.id == reservation_id)).scalar_one()
    session.rollback()
    assert status == "active"  # the status change rolled back with it


def test_successful_hold_and_release_commit_normally(session: Session) -> None:
    with unit_of_work(session):
        reservation_id = new_reservation(session, new_cart(session, "c1"), {"U-S": 2})
        hold_reservation_stock(session, reservation_id, now=NOW)
    with unit_of_work(session):
        end(session, reservation_id, "released")
        release_reservation_stock(session, reservation_id, now=NOW)
    assert stock(session, "U-S") == (5, 0)
    assert_inventory_invariants(session)
    session.rollback()


# --- scope ---------------------------------------------------------------------------------


def test_listener_is_scoped_to_the_project_factory(factory: sessionmaker[Session]) -> None:
    assert event.contains(factory, "before_commit", _refuse_rollback_only_commit)
    assert not event.contains(Session, "before_commit", _refuse_rollback_only_commit)


def test_unit_of_work_protects_sessions_from_other_factories(migrated_database_url: str) -> None:
    # A plain Session has no listener; unit_of_work's own check still refuses the commit.
    engine = create_db_engine(migrated_database_url)
    try:
        with Session(engine) as plain:

            def body() -> None:
                new_cart(plain, "c1")
                mark_rollback_only(plain, "test")

            with pytest.raises(RollbackOnlyTransaction):
                run_unit(plain, body)
            assert rollback_only_reason(plain) is None
            assert counts(plain) == (0, 0, 0)
    finally:
        engine.dispose()
