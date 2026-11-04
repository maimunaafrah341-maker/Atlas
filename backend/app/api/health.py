"""Health endpoints.

``/healthz`` is liveness only and never touches the database, so the platform health check
stays cheap and does not wake or depend on the managed database. ``/api/health`` reports
readiness: database reachability and whether the schema is at the latest migration.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.migrations import current_revision, head_revision

logger = logging.getLogger(__name__)

router = APIRouter()


class Liveness(BaseModel):
    status: Literal["ok"]


class DatabaseHealth(BaseModel):
    status: Literal["ok", "error"]
    dialect: str
    latency_ms: float | None = None
    migration_revision: str | None = None
    migration_head: str | None = None
    schema_up_to_date: bool = False


class HealthReport(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: DatabaseHealth


@router.get("/healthz", response_model=Liveness, tags=["health"])
def liveness() -> Liveness:
    return Liveness(status="ok")


def check_database(engine: Engine) -> DatabaseHealth:
    dialect = engine.dialect.name
    head = head_revision()
    started = time.perf_counter()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = current_revision(connection)
    except SQLAlchemyError as error:
        # Log the error class only: driver messages can include connection details.
        logger.warning("database health check failed", extra={"error_type": type(error).__name__})
        return DatabaseHealth(status="error", dialect=dialect, migration_head=head)
    latency = round((time.perf_counter() - started) * 1000, 1)
    return DatabaseHealth(
        status="ok",
        dialect=dialect,
        latency_ms=latency,
        migration_revision=revision,
        migration_head=head,
        schema_up_to_date=revision == head,
    )


@router.get("/api/health", response_model=HealthReport, tags=["health"])
def readiness(request: Request, response: Response) -> HealthReport:
    settings: Settings = request.app.state.settings
    database = check_database(request.app.state.engine)
    healthy = database.status == "ok" and database.schema_up_to_date
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthReport(
        status="ok" if healthy else "degraded",
        version=settings.version,
        environment=settings.app_env.value,
        database=database,
    )
