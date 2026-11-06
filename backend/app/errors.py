"""Typed application errors and the single JSON error envelope used by every endpoint.

    {"error": {"code": "...", "message": "...", "details": [...]}}

Messages are English; clients localise by ``code`` (ARCHITECTURE §8.4). Validation errors
never echo submitted values.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AtlasError)
    async def atlas_error(_request: Request, error: AtlasError) -> JSONResponse:
        return JSONResponse(error_body(error.code, error.message, error.details), status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {"field": _field_name(item.get("loc", ())), "issue": str(item.get("msg", ""))} for item in error.errors()
        ]
        return JSONResponse(error_body("invalid_request", "The request is invalid.", details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(error.status_code, "error")
        message = error.detail if isinstance(error.detail, str) else "Request failed."
        return JSONResponse(error_body(code, message), status_code=error.status_code, headers=error.headers)
