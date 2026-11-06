"""Shared fixtures.

Database tests run against TEST_DATABASE_URL when it is set (CI sets it to PostgreSQL),
otherwise against a fresh SQLite file per test. PostgreSQL databases are reset to an
empty schema before each test so tests stay independent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Environment, Settings, normalize_database_url
from app.db import create_db_engine
from app.main import create_app
from app.migrations import alembic_config
from app.seed import seed_database

TEST_DATABASE_URL = normalize_database_url(os.environ.get("TEST_DATABASE_URL", "").strip())


def _reset_postgres(url: str) -> None:
    engine = create_db_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    if TEST_DATABASE_URL:
        _reset_postgres(TEST_DATABASE_URL)
        return TEST_DATABASE_URL
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def migrated_database_url(database_url: str) -> str:
    command.upgrade(alembic_config(database_url), "head")
    return database_url


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": Environment.TEST,
        "database_url": database_url,
        "log_level": "WARNING",
        "frontend_dist_dir": None,
        "render_git_commit": "",
    }
    values.update(overrides)
    # _env_file=None: tests never read a developer's local .env.
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def client(migrated_database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(migrated_database_url))) as test_client:
        yield test_client


def open_session(url: str) -> Iterator[Session]:
    engine = create_db_engine(url)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def db_session(migrated_database_url: str) -> Iterator[Session]:
    """A session on an empty, fully migrated database."""
    yield from open_session(migrated_database_url)


@pytest.fixture
def seeded_url(migrated_database_url: str) -> str:
    seed_database(migrated_database_url)
    return migrated_database_url


@pytest.fixture
def seeded_session(seeded_url: str) -> Iterator[Session]:
    yield from open_session(seeded_url)


@pytest.fixture
def seeded_client(seeded_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(seeded_url))) as test_client:
        yield test_client
