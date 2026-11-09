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


def test_reservation_ttl_defaults_to_fifteen_minutes() -> None:
    result = settings()
    assert result.reservation_ttl_minutes == 15
    assert result.reservation_ttl.total_seconds() == 15 * 60


@pytest.mark.parametrize("minutes", [0, -1, 61])
def test_reservation_ttl_out_of_range_is_refused(minutes: int) -> None:
    with pytest.raises(ValidationError, match="reservation_ttl_minutes"):
        settings(reservation_ttl_minutes=minutes)


def test_reservation_ttl_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESERVATION_TTL_MINUTES", "30")
    assert settings().reservation_ttl_minutes == 30


SANDBOX = {
    "paypal_mode": "sandbox",
    "paypal_client_id": "client-id",
    "paypal_client_secret": "client-secret-value",
    "paypal_merchant_id": "ABCDEFGH23456",
}


def test_payments_need_sandbox_mode_and_complete_credentials() -> None:
    assert settings(**SANDBOX).payments_enabled
    assert settings(**{**SANDBOX, "paypal_mode": " Sandbox "}).payments_enabled


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"paypal_mode": ""}, "PAYPAL_MODE"),
        ({"paypal_mode": "live"}, "PAYPAL_MODE"),
        ({"paypal_mode": "production"}, "PAYPAL_MODE"),
        ({"paypal_client_id": ""}, "PAYPAL_CLIENT_ID"),
        ({"paypal_client_secret": " "}, "PAYPAL_CLIENT_SECRET"),
        ({"paypal_merchant_id": ""}, "PAYPAL_MERCHANT_ID"),
        ({"paypal_merchant_id": "abcdefgh23456"}, "PAYPAL_MERCHANT_ID"),
    ],
)
def test_anything_else_disables_payments_without_failing_startup(change: dict[str, str], reason: str) -> None:
    result = settings(**{**SANDBOX, **change})  # never raises: the shop must still start
    assert not result.payments_enabled
    assert result.paypal_problem is not None
    assert reason in result.paypal_problem
    assert "live" not in result.paypal_problem.replace("(Sandbox only", "")  # no configured value echoed


def test_the_client_secret_never_appears_in_repr() -> None:
    result = settings(**SANDBOX)
    assert "client-secret-value" not in repr(result)
    assert "client-secret-value" not in str(result.model_dump())
