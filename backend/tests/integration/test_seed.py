"""Seed behaviour (D-8 / ADR-022): idempotent, create-only, never deletes or overwrites stock,
all-or-nothing, and refuses to run before migrations."""

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import seed as seed_module
from app.config import Settings
from app.domain.catalog.models import Product, ProductTag, ProductTranslation, Variant
from app.domain.inventory.models import InventoryMovement
from app.domain.inventory.service import adjust_stock
from app.domain.shipping.models import ShippingRate, ShippingRateCountry
from app.seed import SEED_REF, SeedError, seed_database
from tests.conftest import open_session
from tests.factories import make_product

TABLES = (Product, ProductTranslation, ProductTag, Variant, InventoryMovement, ShippingRate, ShippingRateCountry)


def counts(session: Session) -> dict[str, int]:
    session.expire_all()
    return {
        model.__tablename__: session.execute(select(func.count()).select_from(model)).scalar_one() for model in TABLES
    }


@pytest.fixture
def session_for(seeded_url: str) -> Iterator[Session]:
    yield from open_session(seeded_url)


def test_first_run_creates_the_catalogue(seeded_session: Session) -> None:
    assert counts(seeded_session) == {
        "products": 20,
        "product_translations": 100,
        "product_tags": sum(1 for _ in seeded_session.execute(select(ProductTag))),
        "variants": 60,
        "inventory_movements": 59,  # one variant starts at zero stock, so it gets no movement
        "shipping_rates": 4,
        "shipping_rate_countries": 29,
    }
    movements = seeded_session.execute(select(InventoryMovement)).scalars().all()
    assert {(movement.reason, movement.ref) for movement in movements} == {("initial_stock", SEED_REF)}


def test_seeded_translations_flags_and_search_fields(seeded_session: Session) -> None:
    for translation in seeded_session.execute(select(ProductTranslation)).scalars():
        assert translation.machine_translated is (translation.lang != "en")
        assert translation.search_fields_consistent()


def test_second_run_changes_nothing(seeded_url: str, seeded_session: Session) -> None:
    before = counts(seeded_session)
    report = seed_database(seeded_url)
    assert sum(report.created.values()) == 0
    assert sum(report.updated.values()) == 0
    assert report.drift == []
    assert counts(seeded_session) == before


def test_reseed_never_overwrites_stock(seeded_url: str, seeded_session: Session) -> None:
    adjust_stock(seeded_session, "ADO-RRS-NAV-M", 5, "restock")
    adjust_stock(seeded_session, "ADO-BWJ-YEL-M", -15, "adjustment")
    seeded_session.commit()
    for update_mode in (False, True):
        seed_database(seeded_url, update=update_mode)
        seeded_session.expire_all()
        stock = dict(seeded_session.execute(select(Variant.sku, Variant.stock_on_hand)).all())
        assert stock["ADO-RRS-NAV-M"] == 8
        assert stock["ADO-BWJ-YEL-M"] == 0


def test_create_only_reports_drift_and_update_restores(seeded_url: str, seeded_session: Session) -> None:
    seeded_session.execute(update(Variant).where(Variant.sku == "ADO-RRS-NAV-M").values(price_minor=1))
    seeded_session.commit()

    report = seed_database(seeded_url)
    assert report.drift == ["variant ADO-RRS-NAV-M: price/threshold differ"]
    assert seeded_session.execute(select(Variant.price_minor).where(Variant.sku == "ADO-RRS-NAV-M")).scalar_one() == 1

    report = seed_database(seeded_url, update=True)
    assert report.updated["variants"] == 1
    seeded_session.expire_all()
    price = seeded_session.execute(select(Variant.price_minor).where(Variant.sku == "ADO-RRS-NAV-M")).scalar_one()
    assert price == 12900


def test_seed_never_deletes(seeded_url: str, seeded_session: Session) -> None:
    make_product(seeded_session, "merchant-added", tags=("camping",))
    rrs = seeded_session.execute(select(Product).where(Product.slug == "ridgeline-rain-shell")).scalar_one()
    seeded_session.add(ProductTag(product_id=rrs.id, tag="travel"))
    seeded_session.commit()
    before = counts(seeded_session)

    report = seed_database(seeded_url, update=True)

    assert counts(seeded_session) == before
    assert any("extra tags ['travel']" in item for item in report.drift)


def test_seed_is_all_or_nothing(migrated_database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated failure after the catalogue was written")

    monkeypatch.setattr(seed_module, "_seed_shipping", explode)
    with pytest.raises(RuntimeError):
        seed_database(migrated_database_url)
    for session in open_session(migrated_database_url):
        assert set(counts(session).values()) == {0}


def test_seed_refuses_unmigrated_schema(database_url: str) -> None:
    with pytest.raises(SeedError, match="not the migration head"):
        seed_database(database_url)


def test_cli_exit_codes(database_url: str, migrated_database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def settings_for(url: str) -> Settings:
        return Settings(_env_file=None, database_url=url, log_level="WARNING")  # type: ignore[call-arg]

    monkeypatch.setattr(seed_module, "get_settings", lambda: settings_for(migrated_database_url))
    assert seed_module.main([]) == 0
    assert seed_module.main([]) == 0  # idempotent


def test_cli_fails_visibly_before_migrations(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, database_url=database_url, log_level="WARNING")  # type: ignore[call-arg]
    monkeypatch.setattr(seed_module, "get_settings", lambda: settings)
    assert seed_module.main([]) == 1
