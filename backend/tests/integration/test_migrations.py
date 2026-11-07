import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import create_db_engine
from app.migrations import alembic_config, current_revision, head_revision


def revision_of(url: str) -> str | None:
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            return current_revision(connection)
    finally:
        engine.dispose()


def test_single_head() -> None:
    from alembic.script import ScriptDirectory

    assert len(ScriptDirectory.from_config(alembic_config()).get_heads()) == 1


def test_upgrade_downgrade_upgrade_round_trip(database_url: str) -> None:
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    assert revision_of(database_url) == head_revision()

    command.downgrade(config, "base")
    assert revision_of(database_url) is None

    command.upgrade(config, "head")
    assert revision_of(database_url) == head_revision()


def test_upgrade_is_idempotent(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = create_db_engine(database_url)
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


# --- 0003_cart_reservations ----------------------------------------------------------------

M1_HEAD = "0002_catalog_inventory_shipping"
M1_MOVEMENT_CHECKS = {
    "ck_inventory_movements_reason",
    "ck_inventory_movements_non_zero_delta",
    "ck_inventory_movements_on_hand_after_non_negative",
    "ck_inventory_movements_reserved_after_non_negative",
    "ck_inventory_movements_available_after_non_negative",
}
M2_MOVEMENT_CHECKS = M1_MOVEMENT_CHECKS | {
    "ck_inventory_movements_reason_deltas",
    "ck_inventory_movements_reservation_link",
}
M1_MOVEMENT_COLUMNS = [
    "id",
    "variant_id",
    "delta_on_hand",
    "delta_reserved",
    "on_hand_after",
    "reserved_after",
    "reason",
    "ref",
    "note",
    "created_at",
]
M2_TABLES = {"carts", "cart_items", "reservations", "reservation_items"}
VARIANT_FK = ("fk_inventory_movements_variant_id_variants", "variants", ("variant_id",))
RESERVATION_FK = ("fk_inventory_movements_reservation_id_reservations", "reservations", ("reservation_id",))


def _movement_rows(url: str) -> list[tuple[object, ...]]:
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            return [
                tuple(row)
                for row in connection.execute(
                    text(
                        "SELECT id, variant_id, delta_on_hand, delta_reserved, on_hand_after, reserved_after, "
                        "reason, ref, note FROM inventory_movements ORDER BY id"
                    )
                )
            ]
    finally:
        engine.dispose()


class MovementSchema:
    """The reflected shape of inventory_movements, plus the table list."""

    def __init__(self, url: str) -> None:
        engine = create_db_engine(url)
        try:
            inspector = inspect(engine)
            self.tables = set(inspector.get_table_names())
            self.columns = [column["name"] for column in inspector.get_columns("inventory_movements")]
            self.checks = {item["name"] for item in inspector.get_check_constraints("inventory_movements")}
            self.indexes = {item["name"] for item in inspector.get_indexes("inventory_movements")}
            self.foreign_keys = {
                (item["name"], item["referred_table"], tuple(item["constrained_columns"]))
                for item in inspector.get_foreign_keys("inventory_movements")
            }
        finally:
            engine.dispose()


M1_PRODUCT = (
    "INSERT INTO products (slug, category, status, sort_order, created_at, updated_at) "
    "VALUES ('migrated', 'jackets', 'active', 0, '2026-11-01 09:00:00', '2026-11-01 09:00:00')"
)
M1_VARIANT = (
    "INSERT INTO variants (product_id, sku, size, colour, price_minor, currency, stock_on_hand, stock_reserved, "
    "low_stock_threshold, status, created_at, updated_at) "
    "SELECT id, 'MIG-1', 'M', 'black', 5000, 'USD', 10, 0, 0, 'active', '2026-11-01 09:00:00', "
    "'2026-11-01 09:00:00' FROM products WHERE slug = 'migrated'"
)
# Columns exactly as 0002 created them: these are genuine M1-era rows. One statement per row:
# a UNION would type the timestamp literals as text, which PostgreSQL won't insert into a
# timestamptz column; a single INSERT ... SELECT types them from the target column.
M1_INITIAL_STOCK = (
    "INSERT INTO inventory_movements (variant_id, delta_on_hand, delta_reserved, on_hand_after, reserved_after, "
    "reason, ref, note, created_at) "
    "SELECT id, 7, 0, 7, 0, 'initial_stock', 'seed:catalog-v1', NULL, '2026-11-01 09:00:00' FROM variants"
)
M1_RESTOCK = (
    "INSERT INTO inventory_movements (variant_id, delta_on_hand, delta_reserved, on_hand_after, reserved_after, "
    "reason, ref, note, created_at) "
    "SELECT id, 3, 0, 10, 0, 'restock', 'po-1', 'supplier delivery', '2026-11-02 09:00:00' FROM variants"
)


def _seed_m1_stock(url: str) -> None:
    """Rows written with raw SQL against the 0002 columns, so this works at any revision."""
    for statement in (M1_PRODUCT, M1_VARIANT, M1_INITIAL_STOCK, M1_RESTOCK):
        _execute(url, statement)


def _execute(url: str, statement: str, params: dict[str, object] | None = None) -> None:
    engine = create_db_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(statement), params or {})
    finally:
        engine.dispose()


CART = (
    "INSERT INTO carts (token_hash, created_at, updated_at, expires_at) "
    "VALUES (:token_hash, '2026-11-07 09:00:00', '2026-11-07 09:00:00', '2026-12-07 09:00:00')"
)
CART_PARAMS = {"token_hash": "a" * 64}
RESERVATION = (
    "INSERT INTO reservations (public_id, cart_id, cart_version, status, created_at, expires_at) "
    "SELECT :public_id, id, 1, 'active', '2026-11-07 09:00:00', '2026-11-07 09:15:00' FROM carts"
)
RESERVATION_PARAMS = {"public_id": "b" * 32}
LINKED_HOLD = (
    "INSERT INTO inventory_movements (variant_id, delta_on_hand, delta_reserved, on_hand_after, "
    "reserved_after, reason, reservation_id, created_at) "
    "SELECT v.id, 0, 1, 10, 1, 'reservation_hold', r.id, CURRENT_TIMESTAMP "
    "FROM variants v, reservations r WHERE v.sku = 'MIG-1'"
)
# Written without reservation_id, so it is valid SQL both before and after 0003.
UNLINKED_HOLD = (
    "INSERT INTO inventory_movements (variant_id, delta_on_hand, delta_reserved, on_hand_after, "
    "reserved_after, reason, created_at) "
    "SELECT id, 0, 1, 10, 1, 'reservation_hold', CURRENT_TIMESTAMP FROM variants WHERE sku = 'MIG-1'"
)


def _hold_stock(url: str) -> None:
    _execute(url, CART, CART_PARAMS)
    _execute(url, RESERVATION, RESERVATION_PARAMS)
    _execute(url, LINKED_HOLD)


def test_upgrade_to_0003_preserves_m1_movements_and_constraints(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, M1_HEAD)
    _seed_m1_stock(database_url)
    before = _movement_rows(database_url)
    assert [row[6] for row in before] == ["initial_stock", "restock"]
    m1 = MovementSchema(database_url)
    assert m1.columns == M1_MOVEMENT_COLUMNS
    assert m1.checks == M1_MOVEMENT_CHECKS

    command.upgrade(config, "0003_cart_reservations")

    assert _movement_rows(database_url) == before  # every M1 row, unchanged
    m2 = MovementSchema(database_url)
    assert m2.tables >= M2_TABLES
    assert sorted(m2.columns) == sorted([*M1_MOVEMENT_COLUMNS, "reservation_id"])
    assert m2.checks == M2_MOVEMENT_CHECKS
    assert m2.indexes == {"ix_inventory_movements_variant_id_created_at", "ix_inventory_movements_reservation_id"}
    assert m2.foreign_keys == {VARIANT_FK, RESERVATION_FK}
    _hold_stock(database_url)  # a linked reservation movement is accepted
    with pytest.raises(IntegrityError):
        _execute(database_url, UNLINKED_HOLD)  # an unlinked one is not


def test_downgrade_refuses_when_reservation_history_exists(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    _seed_m1_stock(database_url)
    _hold_stock(database_url)
    before = _movement_rows(database_url)

    with pytest.raises(RuntimeError, match="reservation audit history"):
        command.downgrade(config, M1_HEAD)

    assert revision_of(database_url) == "0003_cart_reservations"
    schema = MovementSchema(database_url)
    assert schema.tables >= M2_TABLES
    assert schema.checks == M2_MOVEMENT_CHECKS
    assert schema.foreign_keys == {VARIANT_FK, RESERVATION_FK}
    assert _movement_rows(database_url) == before
    assert [row[6] for row in before] == ["initial_stock", "restock", "reservation_hold"]


def test_downgrade_refuses_when_only_a_reservation_exists(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    _execute(database_url, CART, CART_PARAMS)
    _execute(database_url, RESERVATION, RESERVATION_PARAMS)

    with pytest.raises(RuntimeError, match="1 reservation"):
        command.downgrade(config, M1_HEAD)

    assert revision_of(database_url) == "0003_cart_reservations"
    assert MovementSchema(database_url).tables >= M2_TABLES


def test_downgrade_without_reservations_restores_m1_schema(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    _seed_m1_stock(database_url)
    _execute(database_url, CART, CART_PARAMS)  # carts without reservations are not audit history
    before = _movement_rows(database_url)

    command.downgrade(config, M1_HEAD)

    assert revision_of(database_url) == M1_HEAD
    m1 = MovementSchema(database_url)
    assert not m1.tables & M2_TABLES
    assert m1.columns == M1_MOVEMENT_COLUMNS
    assert m1.checks == M1_MOVEMENT_CHECKS
    assert m1.indexes == {"ix_inventory_movements_variant_id_created_at"}
    assert m1.foreign_keys == {VARIANT_FK}
    assert _movement_rows(database_url) == before
    with pytest.raises(IntegrityError):
        _execute(database_url, UNLINKED_HOLD)  # the M1 reason list is back

    command.upgrade(config, "head")
    assert revision_of(database_url) == head_revision()
    assert MovementSchema(database_url).checks == M2_MOVEMENT_CHECKS
    assert _movement_rows(database_url) == before
