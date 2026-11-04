"""Application settings, read only from environment variables (and an optional local .env).

Secrets never have defaults and are never logged. Later milestones add their own settings
here when they start using them; M0 reads only what it needs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
DEFAULT_SQLITE_PATH = BACKEND_DIR / "var" / "atlas.db"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


def normalize_database_url(url: str) -> str:
    """Point PostgreSQL URLs at the psycopg (v3) driver; leave everything else unchanged.

    Managed providers hand out ``postgres://`` or ``postgresql://`` URLs, which SQLAlchemy
    would otherwise map to psycopg2 (not installed).
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def default_sqlite_url() -> str:
    return f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Validation errors must never echo values: DATABASE_URL contains a password.
        hide_input_in_errors=True,
    )

    app_env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    database_url: str = ""
    frontend_dist_dir: Path | None = None
    # Set by Render for each deploy; shown by /api/health so a smoke test can confirm the build.
    render_git_commit: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_database_url(value.strip())

    @field_validator("frontend_dist_dir", mode="before")
    @classmethod
    def _empty_path_is_none(cls, value: object) -> object:
        return None if value in ("", None) else value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @model_validator(mode="after")
    def _check_database_for_environment(self) -> Settings:
        if self.app_env is Environment.PRODUCTION:
            if not self.database_url:
                raise ValueError("DATABASE_URL is required when APP_ENV=production")
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError(
                    "APP_ENV=production requires a PostgreSQL DATABASE_URL; "
                    "SQLite does not survive restarts on the deployment platform"
                )
        elif not self.database_url:
            self.database_url = default_sqlite_url()
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def version(self) -> str:
        return self.render_git_commit[:12] or "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
