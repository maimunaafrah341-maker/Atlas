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
# PayPal's documented webhook id schema (Webhooks v1): alphanumeric, at most 50 characters.
PAYPAL_WEBHOOK_ID_PATTERN = re.compile(r"[A-Za-z0-9]{1,50}")


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
    # M3.3: the id of the Sandbox app's webhook, which PayPal's signature verification API needs.
    # Without it (or without payments) the webhook endpoint fails closed; capture and
    # reconciliation (M3.2) keep working.
    paypal_webhook_id: str = ""
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
    def paypal_webhook_problem(self) -> str | None:
        """Why PayPal webhooks can't be verified (and are refused), or None. Never echoes a value."""
        if self.paypal_problem is not None:
            return self.paypal_problem
        if not PAYPAL_WEBHOOK_ID_PATTERN.fullmatch(self.paypal_webhook_id.strip()):
            return "PAYPAL_WEBHOOK_ID must be the Sandbox webhook's id (alphanumeric, at most 50 characters)"
        return None

    @property
    def webhooks_enabled(self) -> bool:
        return self.paypal_webhook_problem is None

    @property
    def order_hold(self) -> timedelta:
        return timedelta(minutes=self.order_hold_minutes)

    @property
    def version(self) -> str:
        return self.render_git_commit[:12] or "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
