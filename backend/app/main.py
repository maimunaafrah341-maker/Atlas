"""Application factory: API routers, the built single-page app, and shared resources."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import app.models  # noqa: F401  (register every table, so all foreign keys resolve in a fresh process)
from app.api import cart, catalog, checkout, discovery, health, shipping, webhooks
from app.clock import Clock, SystemClock
from app.config import Settings, get_settings
from app.db import create_db_engine, session_factory
from app.domain.discovery.llm import PRIMARY_TIMEOUT, TIMEOUT, GeminiClient, GroqClient, StructuredLLM
from app.domain.payments.paypal import PayPalGateway, SandboxPayPalClient
from app.errors import install_error_handlers
from app.logging_setup import configure_logging
from app.middleware import SafetyMiddleware

logger = logging.getLogger(__name__)


def _mount_spa(app: FastAPI, dist: Path) -> None:
    """Serve the built frontend; client-side routes fall back to index.html.

    Paths under /api never fall back, so an unknown API route is a JSON 404, not HTML.
    """
    index = dist / "index.html"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _paypal_gateway(settings: Settings) -> PayPalGateway | None:
    """The Sandbox client when the configuration is complete; otherwise None (payments 503)."""
    problem = settings.paypal_problem
    if problem is not None:
        configured = settings.paypal_mode.strip() or settings.paypal_client_id.strip()
        (logger.error if configured else logger.info)("payments disabled", extra={"reason": problem})
        return None
    if not settings.webhooks_enabled:
        # Capture and reconcile work without webhooks; the webhook endpoint then answers 503.
        logger.warning("paypal webhooks disabled", extra={"reason": settings.paypal_webhook_problem})
    return SandboxPayPalClient(
        settings.paypal_client_id.strip(), settings.paypal_client_secret.get_secret_value().strip()
    )


def _llms(settings: Settings) -> tuple[StructuredLLM, ...]:
    """The discovery models in order: Gemini, then Groq as the fallback. Empty: keyword parser only."""
    llms: list[StructuredLLM] = []
    problem = settings.llm_problem
    if problem is None:
        logger.info("gemini discovery enabled", extra={"model": settings.resolved_llm_model})
        timeout = PRIMARY_TIMEOUT if settings.groq_problem is None else TIMEOUT
        key = settings.gemini_api_key.get_secret_value().strip()
        llms.append(GeminiClient(key, settings.resolved_llm_model, timeout=timeout))
    else:
        configured = bool(settings.llm_provider.strip())
        (logger.error if configured else logger.info)("gemini discovery disabled", extra={"reason": problem})
    if settings.groq_problem is None:
        logger.info("groq discovery fallback enabled", extra={"model": settings.resolved_groq_model})
        llms.append(GroqClient(settings.groq_api_key.get_secret_value().strip(), settings.resolved_groq_model))
    return tuple(llms)


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    paypal: PayPalGateway | None = None,
    llms: Sequence[StructuredLLM] | None = None,
) -> FastAPI:
    """``paypal`` and ``llms``: tests inject fakes; otherwise they come from the settings."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "atlas starting",
            extra={"environment": settings.app_env.value, "dialect": engine.dialect.name, "version": settings.version},
        )
        yield
        gateway = _app.state.paypal
        if isinstance(gateway, SandboxPayPalClient):
            gateway.close()
        for model in _app.state.llms:
            if isinstance(model, GeminiClient | GroqClient):
                model.close()
        engine.dispose()

    app = FastAPI(
        title="Project Atlas",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = session_factory(engine)
    # The cart and reservation services compare times in SQL against this clock (tests inject one).
    app.state.clock = clock or SystemClock()
    # M3.2: the PayPal gateway, or None when payments are disabled (payment endpoints answer 503).
    app.state.paypal = paypal if paypal is not None else _paypal_gateway(settings)
    # M5: the discovery models in order (Gemini, Groq); empty means the deterministic keyword parser.
    app.state.llms = tuple(llms) if llms is not None else _llms(settings)

    # Outermost: security headers on every response, and the last-resort JSON 500.
    app.add_middleware(SafetyMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(catalog.router)
    app.include_router(shipping.router)
    app.include_router(cart.router)
    app.include_router(checkout.router)
    app.include_router(checkout.config_router)
    app.include_router(discovery.router)
    app.include_router(webhooks.router)

    dist = settings.frontend_dist_dir
    if dist is not None and (dist / "index.html").is_file():
        _mount_spa(app, dist)
    elif dist is not None:
        logger.warning("frontend build not found; serving API only", extra={"frontend_dist_dir": str(dist)})

    return app


def app_factory() -> FastAPI:
    """Entry point for uvicorn: ``uvicorn app.main:app_factory --factory``."""
    return create_app()
