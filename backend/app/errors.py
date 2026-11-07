"""Typed application errors and the single JSON error envelope used by every endpoint.

    {"error": {"code": "...", "message": "...", "details": [...]}}

Messages are English; clients localise by ``code`` (ARCHITECTURE §8.4). Validation errors
never echo submitted values.

Server-side failures (any 5xx) never reach the client with their real message. The client
gets a generic message and a random ``reference``; the real error is logged once, under
that reference, through the redacting JSON formatter.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

logger = logging.getLogger("app.errors")

INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_MESSAGE = "Something went wrong on our side."

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
}


class AtlasError(Exception):
    """Base class for errors that map to a client-visible error code."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []


class InvalidRequest(AtlasError):
    status_code = 422
    code = "invalid_request"

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"Invalid value for {field}: {issue}", details=[{"field": field, "issue": issue}])


def error_body(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or []}}


def _field_name(location: tuple[Any, ...] | list[Any]) -> str:
    parts = [str(part) for part in location]
    if parts and parts[0] in {"query", "path", "body", "header", "cookie"}:
        parts = parts[1:]
    return ".".join(parts) or "request"


def route_pattern(scope: Scope) -> str:
    """The matched route template (e.g. /api/catalog/products/{slug}), never the query string."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def log_server_error(scope: Scope, error: BaseException, *, code: str) -> str:
    """Log a server-side failure exactly once and return its reference for the client."""
    reference = uuid.uuid4().hex
    logger.error(
        "server error",
        exc_info=(type(error), error, error.__traceback__),
        extra={
            "reference": reference,
            "error_code": code,
            "error_type": type(error).__name__,
            "method": scope.get("method", ""),
            "route": route_pattern(scope),
        },
    )
    return reference


def server_error_response(code: str, reference: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse(
        error_body(code, INTERNAL_ERROR_MESSAGE, [{"reference": reference}]),
        status_code=status_code,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AtlasError)
    async def atlas_error(request: Request, error: AtlasError) -> JSONResponse:
        if error.status_code >= 500:
            # An expected server-side failure (e.g. a catalogue integrity problem): the message
            # describes internals, so it is logged, not returned.
            reference = log_server_error(request.scope, error, code=error.code)
            return server_error_response(error.code, reference, error.status_code)
        return JSONResponse(error_body(error.code, error.message, error.details), status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {"field": _field_name(item.get("loc", ())), "issue": str(item.get("msg", ""))} for item in error.errors()
        ]
        return JSONResponse(error_body("invalid_request", "The request is invalid.", details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        if error.status_code >= 500:
            reference = log_server_error(request.scope, error, code=INTERNAL_ERROR_CODE)
            return server_error_response(INTERNAL_ERROR_CODE, reference, error.status_code)
        code = _HTTP_CODES.get(error.status_code, "error")
        message = error.detail if isinstance(error.detail, str) else "Request failed."
        return JSONResponse(error_body(code, message), status_code=error.status_code, headers=error.headers)
