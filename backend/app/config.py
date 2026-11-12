"""Application settings, read only from environment variables (and an optional local .env).

Secrets never have defaults and are never logged. Later milestones add their own settings
here when they start using them; M0 reads only what it needs.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
DEFAULT_SQLITE_PATH = BACKEND_DIR / "var" / "atlas.db"
# PayPal's documented merchant (payer) id format: 13 characters.
PAYPAL_MERCHANT_ID_PATTERN = re.compile(r"^[2-9A-HJ-NP-Z]{13}$")
# M5 (ADR-005): a Gemini model id; it becomes part of the API URL path, so nothing else is allowed.
GEMINI_MODEL_PATTERN = re.compile(r"^gemini-[a-z0-9][a-z0-9.-]{0,62}$")
DEFAULT_LLM_MODEL = "gemini-3.8-flash"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"  # strict structured outputs
GROQ_MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,80}$")


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
    # ADR-003: one store currency. Only USD is supported; anything else is a misconfiguration.
    store_currency: str = "USD"
    # M2 (D3): how long a cart's stock reservation holds before it expires. No renewal.
    reservation_ttl_minutes: int = Field(default=15, ge=1, le=60)
    # M3 (ADR-025): creating an order adopts the cart's reservation and gives it a new, protected
    # deadline this many minutes away. Approved range 15-60.
    order_hold_minutes: int = Field(default=30, ge=15, le=60)
    # M3.2 (ADR-019, ADR-026): PayPal Sandbox only. The API host is hard-coded in
    # app.domain.payments.paypal; there is deliberately no base-URL setting. Payments are enabled
    # only when PAYPAL_MODE is exactly "sandbox" and all three values below are set; otherwise
    # the payment endpoints answer 503 and the rest of the shop keeps working.
    paypal_mode: str = ""
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")  # never logged or shown (SecretStr repr)
    # The Sandbox business account's merchant id; every capture's payee must equal it.
    paypal_merchant_id: str = ""
    # M5 (ADR-005, ADR-028): natural-language discovery. Enabled only when LLM_PROVIDER is
    # "gemini" and GEMINI_API_KEY is set; otherwise discovery uses the keyword parser.
    llm_provider: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    gemini_api_key: SecretStr = SecretStr("")  # never logged or shown (SecretStr repr)
    # The fallback model when Gemini is unavailable: enabled whenever GROQ_API_KEY is set.
    groq_api_key: SecretStr = SecretStr("")  # never logged or shown
    groq_model: str = DEFAULT_GROQ_MODEL
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

    @field_validator("store_currency")
    @classmethod
    def _only_usd(cls, value: str) -> str:
        currency = value.strip().upper()
        if currency != "USD":
            raise ValueError("STORE_CURRENCY must be USD (ADR-003: single store currency)")
        return currency

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
    def reservation_ttl(self) -> timedelta:
        return timedelta(minutes=self.reservation_ttl_minutes)

    @property
    def paypal_problem(self) -> str | None:
        """Why payments are disabled, or None when the Sandbox configuration is complete.

        Never includes a configured value: PAYPAL_MODE might hold anything, and the rest are
        credentials or account identifiers.
        """
        if self.paypal_mode.strip().lower() != "sandbox":
            return "PAYPAL_MODE must be 'sandbox' (Sandbox only, ADR-019)"
        if not (self.paypal_client_id.strip() and self.paypal_client_secret.get_secret_value().strip()):
            return "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required"
        if not PAYPAL_MERCHANT_ID_PATTERN.match(self.paypal_merchant_id.strip()):
            return "PAYPAL_MERCHANT_ID must be the Sandbox business account's 13-character merchant id"
        return None

    @property
    def payments_enabled(self) -> bool:
        return self.paypal_problem is None

    @property
    def llm_problem(self) -> str | None:
        """Why Gemini discovery is off, or None when it is configured. Never includes a value."""
        provider = self.llm_provider.strip().lower()
        if not provider:
            return "LLM_PROVIDER is not set"
        if provider != "gemini":
            return "LLM_PROVIDER must be 'gemini' (the only adapter, ADR-005)"
        if not GEMINI_MODEL_PATTERN.match(self.resolved_llm_model):
            return "LLM_MODEL must be a Gemini model id such as gemini-3.8-flash"
        if not self.gemini_api_key.get_secret_value().strip():
            return "GEMINI_API_KEY is required"
        return None

    @property
    def groq_problem(self) -> str | None:
        """Why the Groq fallback is off, or None. Never includes a value."""
        if not self.groq_api_key.get_secret_value().strip():
            return "GROQ_API_KEY is not set"
        if not GROQ_MODEL_PATTERN.match(self.resolved_groq_model):
            return "GROQ_MODEL must be a Groq model id such as openai/gpt-oss-20b"
        return None

    @property
    def resolved_groq_model(self) -> str:
        return self.groq_model.strip() or DEFAULT_GROQ_MODEL

    @property
    def resolved_llm_model(self) -> str:
        return self.llm_model.strip() or DEFAULT_LLM_MODEL

    @property
    def order_hold(self) -> timedelta:
        return timedelta(minutes=self.order_hold_minutes)

    @property
    def version(self) -> str:
        return self.render_git_commit[:12] or "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
