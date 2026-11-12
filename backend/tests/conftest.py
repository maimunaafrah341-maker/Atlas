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
from app.db import create_db_engine, session_factory
from app.main import create_app
from app.migrations import alembic_config
from app.seed import seed_database

TEST_DATABASE_URL = normalize_database_url(os.environ.get("TEST_DATABASE_URL", "").strip())
# CI's PostgreSQL job sets this: the run then fails unless every test marked ``postgres`` (the
# real-concurrency tests) actually ran and passed. A skip there must never look like success.
REQUIRE_POSTGRES_TESTS = os.environ.get("REQUIRE_POSTGRES_TESTS", "").strip() == "1"
# At least this many postgres-marked tests must pass, so deleting or unmarking one can't
# quietly weaken the guard. Raise it whenever concurrency tests are added.
# 3 (M1, M2.2 primitives) + 12 (reservation concurrency) + 5 (order concurrency) + 4 (payment concurrency)
MIN_POSTGRES_TESTS = 24
_postgres_outcomes: dict[str, str] = {}


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "postgres" not in report.keywords:
        return
    if report.when == "call" or report.outcome != "passed":
        _postgres_outcomes.setdefault(report.nodeid, report.outcome)


def _postgres_guard_problems() -> list[str]:
    problems = []
    if not TEST_DATABASE_URL.startswith("postgresql"):
        problems.append("TEST_DATABASE_URL is not a PostgreSQL URL")
    passed = sum(outcome == "passed" for outcome in _postgres_outcomes.values())
    if passed < MIN_POSTGRES_TESTS:
        problems.append(f"only {passed} postgres-marked tests passed; at least {MIN_POSTGRES_TESTS} are required")
    problems += [
        f"{nodeid}: {outcome}" for nodeid, outcome in sorted(_postgres_outcomes.items()) if outcome != "passed"
    ]
    return problems


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if REQUIRE_POSTGRES_TESTS:
        problems = _postgres_guard_problems()
        passed = sum(outcome == "passed" for outcome in _postgres_outcomes.values())
        terminalreporter.section("PostgreSQL concurrency guard")
        terminalreporter.line(f"{passed} postgres-marked tests passed")
        for problem in problems:
            terminalreporter.line(f"FAIL: {problem}", red=True)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # A GitHub annotation, readable through the public checks API without log access:
            # the run's exact counts and every postgres-marked test that passed.
            stats = terminalreporter.stats
            counts = " ".join(f"{key}={len(stats.get(key, []))}" for key in ("passed", "failed", "error", "skipped"))
            names = ", ".join(
                nodeid.rsplit("::", 1)[-1]
                for nodeid in sorted(_postgres_outcomes)
                if _postgres_outcomes[nodeid] == "passed"
            )
            verdict = "OK" if not problems else "FAIL: " + "; ".join(problems)
            terminalreporter.write_line(
                f"::notice title=PostgreSQL test results::{counts}; postgres-marked passed={passed} "
                f"(minimum {MIN_POSTGRES_TESTS}); guard {verdict}; ran: {names}"
            )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if REQUIRE_POSTGRES_TESTS and _postgres_guard_problems() and session.exitstatus == 0:
        session.exitstatus = 1


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
    # The project's session factory, so tests get the same rollback-only protection as the app.
    engine = create_db_engine(url)
    try:
        with session_factory(engine)() as session:
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
