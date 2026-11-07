"""Application factory: API routers, the built single-page app, and shared resources."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import catalog, health, shipping
from app.config import Settings, get_settings
from app.db import create_db_engine, session_factory
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


def create_app(settings: Settings | None = None) -> FastAPI:
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

    # Outermost: security headers on every response, and the last-resort JSON 500.
    app.add_middleware(SafetyMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(catalog.router)
    app.include_router(shipping.router)

    dist = settings.frontend_dist_dir
    if dist is not None and (dist / "index.html").is_file():
        _mount_spa(app, dist)
    elif dist is not None:
        logger.warning("frontend build not found; serving API only", extra={"frontend_dist_dir": str(dist)})

    return app


def app_factory() -> FastAPI:
    """Entry point for uvicorn: ``uvicorn app.main:app_factory --factory``."""
    return create_app()
