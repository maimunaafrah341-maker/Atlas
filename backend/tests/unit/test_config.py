import pytest
from pydantic import ValidationError

from app.config import Environment, Settings, default_sqlite_url, normalize_database_url


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        ("postgresql://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("sqlite:///./var/atlas.db", "sqlite:///./var/atlas.db"),
    ],
)
def test_normalize_database_url(given: str, expected: str) -> None:
    assert normalize_database_url(given) == expected


def test_development_defaults_to_local_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    result = settings()
    assert result.app_env is Environment.DEVELOPMENT
    assert result.database_url == default_sqlite_url()
    assert result.is_sqlite


def test_production_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        settings(app_env="production")


def test_production_refuses_sqlite() -> None:
    with pytest.raises(ValidationError, match="requires a PostgreSQL"):
        settings(app_env="production", database_url="sqlite:///./atlas.db")


def test_validation_errors_never_echo_the_database_password() -> None:
    with pytest.raises(ValidationError) as caught:
        settings(app_env="production", database_url="mysql://atlas:hunter2-secret@db.example.com/atlas")
    assert "hunter2-secret" not in str(caught.value)


def test_production_accepts_postgres_and_normalizes() -> None:
    result = settings(app_env="production", database_url="postgres://u:p@host/db?sslmode=require")
    assert result.database_url.startswith("postgresql+psycopg://")
    assert not result.is_sqlite


def test_empty_frontend_dist_dir_means_api_only() -> None:
    assert settings(frontend_dist_dir="").frontend_dist_dir is None


def test_version_uses_render_commit() -> None:
    assert settings(render_git_commit="0123456789abcdef0123").version == "0123456789ab"
    assert settings(render_git_commit="").version == "dev"
