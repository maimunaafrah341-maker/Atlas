"""HTTP smoke test for a running Atlas instance (local, container, or deployed).

Standard library only, so it runs anywhere Python 3.11+ is available:

    python scripts/smoke_test.py http://127.0.0.1:8000
    python scripts/smoke_test.py https://<service>.onrender.com --expect-dialect postgresql

--cart also exercises the cart API (add, reserve, release, remove). It writes a cart, so it
refuses to run against anything but a local instance: CI runs it against the Docker container,
never against production.

A Render free instance may be asleep; the first request can take about a minute, so the
liveness check retries until --wake-timeout seconds have passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "atlas-smoke-test/1.0"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: str
    set_cookies: list[str] | None = None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        return json.loads(self.body)

    def json_or_empty(self) -> dict[str, Any]:
        try:
            value = self.json()
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


def fetch(
    url: str,
    timeout: float,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
) -> Result:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            cookies = response.headers.get_all("set-cookie") or []
            return Result(response.status, response_headers, response.read().decode(), cookies)
    except urllib.error.HTTPError as error:
        response_headers = {key.lower(): value for key, value in error.headers.items()}
        cookies = error.headers.get_all("set-cookie") or []
        return Result(error.code, response_headers, error.read().decode(), cookies)


def wait_until_live(base: str, wake_timeout: float) -> float:
    started = time.monotonic()
    last_error = ""
    while time.monotonic() - started < wake_timeout:
        try:
            result = fetch(f"{base}/healthz", timeout=30)
            if result.status == 200:
                return time.monotonic() - started
            last_error = f"HTTP {result.status}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = type(error).__name__
        time.sleep(3)
    raise SystemExit(f"FAIL /healthz did not become live within {wake_timeout:.0f}s ({last_error})")


def cart_checks(base: str, environment: str, check: Any) -> None:
    """Add, reserve, release and remove one item, as a browser on the site's own origin would.

    The Origin follows the server's rule: https in production (Render terminates TLS), the
    connection's own scheme otherwise. The cookie is carried by hand, because a Secure cookie
    is not returned over the container's plain-http connection.
    """
    parts = urllib.parse.urlsplit(base)
    if parts.hostname not in LOCAL_HOSTS:
        raise SystemExit("FAIL --cart writes a cart and only runs against a local instance, never production")
    scheme = "https" if environment == "production" else parts.scheme
    origin = f"{scheme}://{parts.netloc}"

    listing = fetch(f"{base}/api/catalog/products?in_stock=true&page_size=1", timeout=30).json_or_empty()
    variants = (listing.get("items") or [{}])[0].get("variants") or []
    sku = next((v["sku"] for v in variants if v.get("stock_status") != "out_of_stock"), None)
    check("cart: an in-stock SKU to add", sku is not None, str(sku))
    if sku is None:
        return

    token = ""

    def call(method: str, path: str, body: Any = None, *, send_origin: str | None = origin) -> Result:
        headers = {"Cookie": f"atlas_cart={token}"} if token else {}
        if send_origin:
            headers["Origin"] = send_origin
        return fetch(f"{base}{path}", timeout=30, method=method, headers=headers, body=body)

    item = f"/api/cart/items/{urllib.parse.quote(sku)}"
    foreign = call("PUT", item, {"quantity": 1}, send_origin="https://evil.example")
    foreign_code = (foreign.json_or_empty().get("error") or {}).get("code")
    check(
        "cart: cross-site change refused",
        foreign.status == 403 and foreign_code == "cross_origin_request",
        f"HTTP {foreign.status} {foreign_code}",
    )

    added = call("PUT", item, {"quantity": 1})
    cookie = next((c for c in added.set_cookies or [] if c.startswith("atlas_cart=")), "")
    token = cookie.split(";", 1)[0].partition("=")[2]
    attributes = {part.strip().split("=", 1)[0].lower() for part in cookie.split(";")[1:]}
    added_lines = added.json_or_empty().get("lines") or []
    check("cart: add item", added.status == 200 and len(added_lines) == 1, f"HTTP {added.status}")
    check(
        "cart: cookie is HttpOnly, SameSite, path-scoped (Secure in production)",
        bool(token)
        and {"httponly", "samesite", "path"} <= attributes
        and ("secure" in attributes) == (environment == "production"),
        ", ".join(sorted(attributes)),
    )
    check("cart: no-store", added.headers.get("cache-control") == "no-store")

    read = call("GET", "/api/cart")
    check("cart: read back", read.status == 200 and len(read.json_or_empty().get("lines") or []) == 1)

    first = call("POST", "/api/cart/reservation")
    again = call("POST", "/api/cart/reservation")
    reservation = first.json_or_empty().get("reservation") or {}
    check("cart: reserve", first.status == 201 and reservation.get("status") == "active", f"HTTP {first.status}")
    check(
        "cart: repeated reserve returns the same reservation, not renewed",
        again.status == 200 and again.json_or_empty().get("reservation") == reservation,
        f"HTTP {again.status}",
    )

    released = call("DELETE", "/api/cart/reservation")
    check("cart: release", released.status == 200 and released.json_or_empty().get("reservation") is None)
    removed = call("DELETE", item)
    check("cart: remove item", removed.status == 200 and removed.json_or_empty().get("lines") == [])

    bodies = "".join(r.body for r in (added, read, first, again, released, removed))
    check("cart: token never in a response body", bool(token) and token not in bodies)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("--expect-dialect", choices=["sqlite", "postgresql"])
    parser.add_argument("--wake-timeout", type=float, default=120)
    parser.add_argument("--no-frontend", action="store_true", help="skip SPA checks (API-only instance)")
    parser.add_argument(
        "--skip-catalog", action="store_true", help="skip catalogue/shipping checks (unseeded instance)"
    )
    parser.add_argument(
        "--cart", action="store_true", help="also exercise the cart API (writes a cart; local instances only)"
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if args.cart and urllib.parse.urlsplit(base).hostname not in LOCAL_HOSTS:
        # Checked before any request: the cart checks write data and must never reach production.
        raise SystemExit("FAIL --cart writes a cart and only runs against a local instance, never production")

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
        if not ok:
            failures.append(name)

    waited = wait_until_live(base, args.wake_timeout)
    check("liveness /healthz", True, f"live after {waited:.1f}s")

    health = fetch(f"{base}/api/health", timeout=30)
    try:
        report = health.json()
    except ValueError:
        report = {}
    database = report.get("database", {})
    check("readiness /api/health returns 200", health.status == 200, f"HTTP {health.status}")
    check("database reachable", database.get("status") == "ok", str(database.get("status")))
    check(
        "schema at migration head",
        database.get("schema_up_to_date") is True,
        f"{database.get('migration_revision')} / {database.get('migration_head')}",
    )
    if args.expect_dialect:
        check(
            f"database dialect is {args.expect_dialect}",
            database.get("dialect") == args.expect_dialect,
            str(database.get("dialect")),
        )
    print(f"     version={report.get('version')} environment={report.get('environment')}")

    unknown = fetch(f"{base}/api/smoke-test-unknown-route", timeout=30)
    check(
        "unknown API route is a JSON 404",
        unknown.status == 404 and unknown.content_type.startswith("application/json"),
        f"HTTP {unknown.status} {unknown.content_type}",
    )

    if not args.skip_catalog:
        catalog = fetch(f"{base}/api/catalog/products?page_size=1", timeout=30)
        body = catalog.json_or_empty()
        check(
            "catalogue lists products",
            catalog.status == 200 and isinstance(body.get("total"), int) and body["total"] > 0,
            f"HTTP {catalog.status} total={body.get('total')}",
        )
        items = body.get("items") or [{}]
        price = (items[0].get("price_range") or {}).get("min") or {}
        check(
            "prices are integer USD minor units",
            isinstance(price.get("amount_minor"), int) and price.get("currency") == "USD",
            str(price),
        )
        france = fetch(f"{base}/api/shipping/quote?country=FR", timeout=30)
        quote = france.json_or_empty().get("price") or {}
        check(
            "shipping quote for FR",
            france.status == 200 and isinstance(quote.get("amount_minor"), int) and quote.get("currency") == "USD",
            f"HTTP {france.status}",
        )
        india = fetch(f"{base}/api/shipping/quote?country=IN", timeout=30)
        error_code = (india.json_or_empty().get("error") or {}).get("code")
        check(
            "unsupported destination is a typed error",
            india.status == 422 and error_code == "unsupported_destination",
            f"HTTP {india.status} {error_code}",
        )

    if args.cart:
        cart_checks(base, str(report.get("environment")), check)

    if not args.no_frontend:
        index = fetch(f"{base}/", timeout=30)
        check("SPA index served", index.status == 200 and '<div id="root">' in index.body, f"HTTP {index.status}")
        deep = fetch(f"{base}/some/client/route", timeout=30)
        check("SPA client route falls back to index", deep.status == 200 and '<div id="root">' in deep.body)
        check(
            "security headers present",
            index.headers.get("x-content-type-options") == "nosniff" and index.headers.get("x-frame-options") == "DENY",
        )

    print(f"\n{'SMOKE TEST PASSED' if not failures else f'SMOKE TEST FAILED: {len(failures)} check(s)'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
