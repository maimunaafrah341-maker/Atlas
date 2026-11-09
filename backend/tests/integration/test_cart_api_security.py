"""Cross-site request protection and production cookie attributes for /api/cart (ARCHITECTURE §6).

The expected origin is the trusted scheme (always https in production; the connection's own
elsewhere) plus the Host header. X-Forwarded-Proto is never used for it: production runs uvicorn
with --forwarded-allow-ips '*', so that header is trusted from any client there.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.security import normalize_origin
from app.clock import FixedClock
from app.config import Environment
from app.db import create_db_engine, session_factory
from app.main import create_app
from tests.cart_api_support import ORIGIN, build_shop, cart_count, cookie_attributes, set_cookie_headers
from tests.conftest import make_settings
from tests.reservation_builders import NOW

CHANGES = [
    ("PUT", "/api/cart/items/SOCK-M", {"quantity": 1}),
    ("DELETE", "/api/cart/items/SOCK-M", None),
    ("POST", "/api/cart/reservation", None),
    ("DELETE", "/api/cart/reservation", None),
]


@pytest.fixture
def shop_url(migrated_database_url: str) -> str:
    engine = create_db_engine(migrated_database_url)
    with session_factory(engine)() as session:
        build_shop(session)
    engine.dispose()
    return migrated_database_url


@pytest.fixture
def db(shop_url: str) -> Iterator[Session]:
    engine = create_db_engine(shop_url)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


def make_client(shop_url: str, *, production: bool = False, base_url: str = "http://testserver") -> TestClient:
    settings = make_settings(shop_url)
    app = create_app(settings, clock=FixedClock(NOW))
    if production:
        # Production settings require PostgreSQL, so switch only the environment after start-up;
        # the cart dependencies read app.state.settings on every request.
        app.state.settings = settings.model_copy(update={"app_env": Environment.PRODUCTION})
    return TestClient(app, base_url=base_url)


@pytest.fixture
def api(shop_url: str) -> Iterator[TestClient]:
    with make_client(shop_url) as client:
        yield client


def send(
    client: TestClient, method: str, path: str, body: Any, headers: dict[str, str] | None = None
) -> httpx.Response:
    return client.request(method, path, json=body, headers=headers)


def refused(response: httpx.Response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "cross_origin_request"
    assert response.headers["cache-control"] == "no-store"


# --- Origin normalisation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://testserver", "http://testserver"),
        ("HTTP://TestServer", "http://testserver"),
        ("http://testserver:80", "http://testserver"),
        ("https://example.com:443", "https://example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("http://[::1]:5173", "http://[::1]:5173"),
        ("http://testserver/", "http://testserver"),
        ("http://testserver/path", None),
        ("http://testserver?x=1", None),
        ("http://user:pw@testserver", None),
        ("ftp://testserver", None),
        ("null", None),
        ("", None),
        ("http://testserver:99999", None),
    ],
)
def test_normalize_origin(value: str, expected: str | None) -> None:
    assert normalize_origin(value) == expected


# --- Refused requests ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), CHANGES)
@pytest.mark.parametrize(
    "headers",
    [
        {},  # no Origin at all
        {"Origin": "null"},  # sandboxed or opaque origin
        {"Origin": "https://evil.example"},  # another site
        {"Origin": "https://testserver"},  # scheme mismatch
        {"Origin": "http://testserver:8080"},  # port mismatch
        {"Origin": "http://testserver.evil.example"},  # host mismatch
        {"Origin": "http://testserver/cart"},  # not an origin
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "none"},
    ],
)
def test_changes_from_anywhere_but_the_site_are_refused(
    api: TestClient, db: Session, method: str, path: str, body: Any, headers: dict[str, str]
) -> None:
    refused(send(api, method, path, body, headers))
    assert cart_count(db) == 0


def test_two_origin_headers_are_refused(api: TestClient) -> None:
    response = api.request(
        "PUT",
        "/api/cart/items/SOCK-M",
        json={"quantity": 1},
        headers=[("Origin", ORIGIN), ("Origin", "https://evil.example")],
    )
    refused(response)


def test_the_origin_check_runs_before_body_validation(api: TestClient) -> None:
    refused(send(api, "PUT", "/api/cart/items/SOCK-M", {"quantity": "nonsense"}, {"Origin": "https://evil.example"}))


def test_a_forged_forwarded_proto_does_not_change_the_expected_scheme(api: TestClient) -> None:
    refused(
        send(
            api,
            "PUT",
            "/api/cart/items/SOCK-M",
            {"quantity": 1},
            {"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
        )
    )


def test_a_forwarded_host_is_ignored(api: TestClient) -> None:
    refused(
        send(
            api,
            "PUT",
            "/api/cart/items/SOCK-M",
            {"quantity": 1},
            {"Origin": "http://evil.example", "X-Forwarded-Host": "evil.example"},
        )
    )


# --- Accepted requests ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": ORIGIN},
        {"Origin": "HTTP://TESTSERVER"},
        {"Origin": "http://testserver:80"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
    ],
)
def test_same_origin_changes_are_accepted(api: TestClient, headers: dict[str, str]) -> None:
    assert send(api, "PUT", "/api/cart/items/SOCK-M", {"quantity": 1}, headers).status_code == 200


def test_a_non_default_port_must_match_exactly(shop_url: str) -> None:
    with make_client(shop_url, base_url="http://testserver:5173") as client:
        ok = send(client, "PUT", "/api/cart/items/SOCK-M", {"quantity": 1}, {"Origin": "http://testserver:5173"})
        assert ok.status_code == 200
        refused(send(client, "PUT", "/api/cart/items/SOCK-M", {"quantity": 2}, {"Origin": "http://testserver"}))
        refused(send(client, "PUT", "/api/cart/items/SOCK-M", {"quantity": 2}, {"Origin": "http://testserver:5174"}))


def test_reading_the_cart_needs_no_origin(api: TestClient) -> None:
    assert api.get("/api/cart").status_code == 200
    assert api.get("/api/cart", headers={"Origin": "https://evil.example"}).status_code == 200


# --- Production -------------------------------------------------------------------------------------


def test_production_expects_https_whatever_the_connection_says(shop_url: str) -> None:
    with make_client(shop_url, production=True) as client:
        # Render terminates TLS; the app sees plain http but the site's origin is https.
        accepted = send(client, "PUT", "/api/cart/items/SOCK-M", {"quantity": 1}, {"Origin": "https://testserver"})
        assert accepted.status_code == 200
        for headers in (
            {"Origin": "http://testserver"},
            {"Origin": "http://testserver", "X-Forwarded-Proto": "http"},
        ):
            refused(send(client, "PUT", "/api/cart/items/SOCK-M", {"quantity": 2}, headers))


def test_production_cookies_are_secure_when_set_and_deleted(shop_url: str) -> None:
    with make_client(shop_url, production=True) as client:
        added = send(client, "PUT", "/api/cart/items/SOCK-M", {"quantity": 1}, {"Origin": "https://testserver"})
        [header] = set_cookie_headers(added)
        name, _token, attributes = cookie_attributes(header)
        assert name == "atlas_cart"
        assert {"secure", "httponly"} <= set(attributes)
        assert (attributes["path"], attributes["samesite"]) == ("/api/cart", "lax")

        stale = client.get("/api/cart", headers={"Cookie": "atlas_cart=unknown-token"})
        [deleted] = set_cookie_headers(stale)
        _, _, deleted_attributes = cookie_attributes(deleted)
        assert {"secure", "httponly"} <= set(deleted_attributes)
        assert (deleted_attributes["path"], deleted_attributes["samesite"], deleted_attributes["max-age"]) == (
            "/api/cart",
            "lax",
            "0",
        )
