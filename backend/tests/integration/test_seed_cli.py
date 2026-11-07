"""The real seed entrypoints, each in a fresh interpreter.

In-process seed tests can pass because some other test module already imported
``app.models``, which registers every table and foreign key. Production runs
``python -m app.seed`` (and ``scripts/start.sh``) in a fresh process that imports only what
the seed itself imports, so these tests do the same with subprocesses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select

from app.config import BACKEND_DIR, REPO_DIR
from app.db import create_db_engine
from app.domain.catalog.models import Product, ProductTranslation, Variant
from app.domain.inventory.models import InventoryMovement
from app.migrations import alembic_config, current_revision, head_revision
from app.seed import CATALOG_FILE, load_seeds

SH = shutil.which("sh")
START = REPO_DIR / "scripts" / "start.sh"


def _expected() -> tuple[int, int, int, int]:
    catalog, _shipping = load_seeds()
    variants = [variant for product in catalog.products for variant in product.variants]
    translations = sum(len(product.translations) for product in catalog.products)
    stocked = sum(1 for variant in variants if variant.initial_stock > 0)
    return len(catalog.products), len(variants), translations, stocked


def _env(database_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "APP_ENV": "development",
        "LOG_LEVEL": "INFO",
        "DATABASE_URL": database_url,
        "STORE_CURRENCY": "USD",
        "FRONTEND_DIST_DIR": "",
    }


def _assert_seeded(database_url: str) -> None:
    products, variants, translations, stocked = _expected()
    engine = create_db_engine(database_url)
    try:
        with engine.connect() as connection:
            assert current_revision(connection) == head_revision()
            assert connection.execute(select(func.count()).select_from(Product)).scalar_one() == products
            assert connection.execute(select(func.count()).select_from(Variant)).scalar_one() == variants
            assert connection.execute(select(func.count()).select_from(ProductTranslation)).scalar_one() == translations
            movements = connection.execute(
                select(InventoryMovement.reason, InventoryMovement.reservation_id, InventoryMovement.ref)
            ).all()
    finally:
        engine.dispose()
    # One initial_stock movement per stocked variant; stock adjustments never name a reservation.
    assert len(movements) == stocked
    assert {(reason, reservation_id, ref) for reason, reservation_id, ref in movements} == {
        ("initial_stock", None, "seed:catalog-v1")
    }


def test_seed_cli_in_a_fresh_process(migrated_database_url: str) -> None:
    assert CATALOG_FILE.is_file()
    result = subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=BACKEND_DIR,
        env=_env(migrated_database_url),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"catalogue seed complete"' in result.stdout
    _assert_seeded(migrated_database_url)


def test_fresh_process_imports_register_every_table() -> None:
    # Both entrypoints must register all tables, so foreign keys such as
    # inventory_movements.reservation_id resolve on the first flush.
    script = (
        "import sys; import {module}; from app.db import Base; "
        "missing = {{'carts', 'cart_items', 'reservations', 'reservation_items'}} - set(Base.metadata.tables); "
        "sys.exit(1 if missing else 0)"
    )
    for module in ("app.seed", "app.main"):
        result = subprocess.run(  # noqa: S603 (fixed command)
            [sys.executable, "-c", script.format(module=module)],
            cwd=BACKEND_DIR,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"{module}: {result.stdout}{result.stderr}"


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh")
def test_start_script_migrates_and_seeds_a_fresh_database(tmp_path: Path) -> None:
    # The production startup path (SEED_CATALOG_ON_START=1) with the real alembic and seed.
    # Only uvicorn is stubbed, so the test stops where the server would start.
    database_url = f"sqlite:///{(tmp_path / 'startup.db').as_posix()}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "uvicorn-called"
    stub = bin_dir / "uvicorn"
    stub.write_text(f'#!/bin/sh\necho "$*" > "{marker.as_posix()}"\nexit 0\n', encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    python_dir = Path(sys.executable).parent  # the interpreter (and alembic) running these tests
    env = {
        **_env(database_url),
        "PATH": os.pathsep.join([str(bin_dir), str(python_dir), os.environ.get("PATH", "")]),
        "ATLAS_BACKEND_DIR": str(BACKEND_DIR),
        "SEED_CATALOG_ON_START": "1",
    }
    result = subprocess.run(  # noqa: S603 (fixed command: sh and the repository's start script)
        [str(SH), str(START)], env=env, capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"catalogue seed complete"' in result.stdout
    assert marker.read_text(encoding="utf-8").startswith("app.main:app_factory")
    _assert_seeded(database_url)


def test_seed_cli_fails_cleanly_before_migrations(tmp_path: Path) -> None:
    # Unchanged safeguard, now exercised through the real entrypoint too.
    database_url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    command.upgrade(alembic_config(database_url), "0002_catalog_inventory_shipping")
    result = subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=BACKEND_DIR,
        env=_env(database_url),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    assert "not the migration head" in result.stderr
