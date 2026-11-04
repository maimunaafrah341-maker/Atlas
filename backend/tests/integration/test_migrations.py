from alembic import command
from sqlalchemy import inspect

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
