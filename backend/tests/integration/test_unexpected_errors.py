"""Unexpected errors through the real middleware stack (create_app), not a handler in isolation.

Each test route plants secrets in the exception, SQL parameters or response data, then checks:
the client gets the standard JSON 500 with a reference and the security headers; nothing
internal reaches the client; the error is logged exactly once, redacted, under that reference.
TestClient runs with raise_server_exceptions=True, so an exception escaping the middleware
(or being re-raised) fails the test.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import SessionDep
from app.domain.catalog.schemas import Money
from app.domain.catalog.service import CatalogIntegrityError
from app.logging_setup import JsonFormatter
from app.main import create_app
from app.middleware import SECURITY_HEADERS
from tests.conftest import make_settings

SECRET = "hunter2"
PARAM_SECRET = "tok-param-value-hunter2"
RESPONSE_SECRET = "SECRETVALUE"
EMAIL = "buyer@example.com"
LEAKS = (SECRET, PARAM_SECRET, RESPONSE_SECRET, EMAIL, "Traceback", "RuntimeError", "SELECT", "no_such_table")
REFERENCE = re.compile(r"^[0-9a-f]{32}$")


def _boom() -> None:
    raise RuntimeError(f"internal detail postgresql://admin:{SECRET}@db.internal/atlas for {EMAIL}")


def _sql(session: SessionDep) -> None:
    session.execute(text("SELECT secret_column FROM no_such_table WHERE token = :token"), {"token": PARAM_SECRET})


def _bad_response() -> Any:
    return {"amount_minor": f"not-an-int-{RESPONSE_SECRET}", "currency": "USD"}


def _stream() -> StreamingResponse:
    def body() -> Iterator[bytes]:
        yield b"partial"
        raise RuntimeError(f"mid-stream failure {SECRET}")

    return StreamingResponse(body(), media_type="text/plain")


def _integrity() -> None:
    raise CatalogIntegrityError(f"Product secret-slug has no English text ({SECRET}).")


def _http_500() -> None:
    raise HTTPException(status_code=500, detail=f"database password {SECRET}")


def _install_test_routes(app: FastAPI) -> None:
    app.add_api_route("/api/test/boom", _boom)
    app.add_api_route("/api/test/sql", _sql)
    app.add_api_route("/api/test/bad-response", _bad_response, response_model=Money)
    app.add_api_route("/api/test/stream", _stream)
    app.add_api_route("/api/test/integrity", _integrity)
    app.add_api_route("/api/test/http-500", _http_500)


class LogBuffer:
    """The app's own JSON log handler (real formatter and redaction), redirected to memory."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    def read(self) -> tuple[list[dict[str, Any]], str]:
        out = self.stream.getvalue()
        self.stream.seek(0)
        self.stream.truncate()
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        return [record for record in records if record["level"] in {"ERROR", "CRITICAL"}], out


@pytest.fixture
def logs() -> LogBuffer:
    return LogBuffer()


@pytest.fixture
def error_client(migrated_database_url: str, logs: LogBuffer) -> Iterator[TestClient]:
    app = create_app(make_settings(migrated_database_url))
    [handler] = [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(logs.stream)
    _install_test_routes(app)
    with TestClient(app, raise_server_exceptions=True) as test_client:
        logs.read()  # discard startup output
        yield test_client


def assert_generic_500(response: Any, code: str = "internal_error") -> str:
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"] == "Something went wrong on our side."
    [detail] = body["error"]["details"]
    assert set(detail) == {"reference"}
    assert REFERENCE.match(detail["reference"])
    for leak in LEAKS:
        assert leak not in response.text
    reference: str = detail["reference"]
    return reference


@pytest.mark.parametrize(
    ("path", "error_type"),
    [
        ("/api/test/boom", "RuntimeError"),
        ("/api/test/sql", "OperationalError|ProgrammingError"),
        ("/api/test/bad-response", "ResponseValidationError"),
    ],
)
def test_unexpected_error_is_generic_json_500_logged_once(
    error_client: TestClient, logs: LogBuffer, path: str, error_type: str
) -> None:
    reference = assert_generic_500(error_client.get(path, params={"q": EMAIL}))

    errors, out = logs.read()
    assert len(errors) == 1, out
    [record] = errors
    assert record["reference"] == reference
    assert record["route"] == path
    assert record["method"] == "GET"
    assert re.fullmatch(error_type, record["error_type"])
    assert "exception" in record  # the traceback is kept for operators, redacted
    # Credentials, emails and SQL parameter values never reach the log; the query string is not logged.
    assert SECRET not in out
    assert EMAIL not in out
    assert PARAM_SECRET not in out


def test_sql_error_logs_statement_without_parameters(error_client: TestClient, logs: LogBuffer) -> None:
    assert_generic_500(error_client.get("/api/test/sql"))
    [record], _out = logs.read()
    assert "no_such_table" in record["exception"]  # useful to operators
    assert PARAM_SECRET not in record["exception"]  # hide_parameters=True


def test_expected_server_error_hides_its_message(error_client: TestClient, logs: LogBuffer) -> None:
    response = error_client.get("/api/test/integrity")
    reference = assert_generic_500(response, code="catalog_integrity_error")
    assert "secret-slug" not in response.text
    [record], _out = logs.read()
    assert record["reference"] == reference
    assert record["error_code"] == "catalog_integrity_error"
    assert "secret-slug" in record["exception"]  # the real message is kept for operators


def test_http_exception_500_hides_detail(error_client: TestClient, logs: LogBuffer) -> None:
    reference = assert_generic_500(error_client.get("/api/test/http-500"))
    [record], _out = logs.read()
    assert record["reference"] == reference


def test_error_after_response_started_is_logged_once_and_not_reraised(
    error_client: TestClient, logs: LogBuffer
) -> None:
    # Headers were already sent, so the status cannot become 500. The middleware logs the
    # error once and does not re-raise (raise_server_exceptions=True would fail this test).
    response = error_client.get("/api/test/stream")
    assert response.status_code == 200
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert SECRET not in response.text
    [record], _out = logs.read()
    assert record["route"] == "/api/test/stream"
    assert record["error_type"] == "RuntimeError"
    assert "mid-stream failure" in record["exception"]  # the operator sees the cause


def test_each_error_gets_a_new_reference(error_client: TestClient) -> None:
    first = error_client.get("/api/test/boom").json()["error"]["details"][0]["reference"]
    second = error_client.get("/api/test/boom").json()["error"]["details"][0]["reference"]
    assert first != second


def test_ordinary_errors_keep_their_format_and_headers(error_client: TestClient, logs: LogBuffer) -> None:
    not_found = error_client.get("/api/catalog/products/no-such-product")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "product_not_found"
    invalid = error_client.get("/api/catalog/products", params={"page_size": 0})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
    unknown = error_client.get("/api/nope")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    for response in (not_found, invalid, unknown):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
    errors, _out = logs.read()
    assert errors == []
