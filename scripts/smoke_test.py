"""HTTP smoke test for a running Atlas instance (local, container, or deployed).

Standard library only, so it runs anywhere Python 3.11+ is available:

    python scripts/smoke_test.py http://127.0.0.1:8000
    python scripts/smoke_test.py https://<service>.onrender.com --expect-dialect postgresql

A Render free instance may be asleep; the first request can take about a minute, so the
liveness check retries until --wake-timeout seconds have passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "atlas-smoke-test/1.0"


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: str

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        return json.loads(self.body)


def fetch(url: str, timeout: float) -> Result:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            headers = {key.lower(): value for key, value in response.headers.items()}
            return Result(response.status, headers, response.read().decode())
    except urllib.error.HTTPError as error:
        headers = {key.lower(): value for key, value in error.headers.items()}
        return Result(error.code, headers, error.read().decode())


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("--expect-dialect", choices=["sqlite", "postgresql"])
    parser.add_argument("--wake-timeout", type=float, default=120)
    parser.add_argument("--no-frontend", action="store_true", help="skip SPA checks (API-only instance)")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

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
