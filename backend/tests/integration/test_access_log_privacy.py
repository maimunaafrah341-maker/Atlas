"""Access-log privacy under a real uvicorn process (not a mocked logger).

The app is started the way scripts/start.sh starts it (uvicorn, --factory, the app's own
configure_logging). A request carries a distinctive synthetic search term in ``?q=``; the
captured server output must never contain it, while application logs keep working.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.config import BACKEND_DIR

SEARCH_TERM = "zqxprivacyprobe7731"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _run_server_and_search(database_url: str, log_path: Path, *, access_log: bool) -> str:
    port = _free_port()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app_factory",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if not access_log:
        command.append("--no-access-log")
    env = {
        **os.environ,
        "APP_ENV": "development",
        "LOG_LEVEL": "INFO",
        "DATABASE_URL": database_url,
        "FRONTEND_DIST_DIR": "",
    }
    base = f"http://127.0.0.1:{port}"
    with log_path.open("wb") as log_file:
        # Fixed command (this interpreter + uvicorn); no external input.
        server = subprocess.Popen(command, cwd=BACKEND_DIR, env=env, stdout=log_file, stderr=subprocess.STDOUT)  # noqa: S603
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urllib.request.urlopen(f"{base}/healthz", timeout=2):  # noqa: S310 (local http only)
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        pytest.fail(f"uvicorn did not start:\n{log_path.read_text(encoding='utf-8', errors='replace')}")
                    time.sleep(0.2)
            search_url = f"{base}/api/catalog/products?q={SEARCH_TERM}&page_size=5"
            with urllib.request.urlopen(search_url, timeout=10) as reply:  # noqa: S310 (local http only)
                assert reply.status == 200
            time.sleep(0.5)  # let the access line flush
        finally:
            server.terminate()
            server.wait(timeout=15)
    return log_path.read_text(encoding="utf-8", errors="replace")


def _json_records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def test_production_flags_log_no_requests_and_no_search_terms(migrated_database_url: str, tmp_path: Path) -> None:
    # --no-access-log, as in scripts/start.sh: access logging stays off despite configure_logging().
    output = _run_server_and_search(migrated_database_url, tmp_path / "server.log", access_log=False)
    assert SEARCH_TERM not in output
    records = _json_records(output)
    assert not [record for record in records if record["logger"] == "uvicorn.access"], output
    # Application and server logs still work.
    assert any(record["message"] == "atlas starting" for record in records), output
    assert any(record["logger"] == "uvicorn.error" for record in records), output


def test_enabled_access_log_keeps_path_but_drops_query_string(migrated_database_url: str, tmp_path: Path) -> None:
    # Local development leaves access logging on: lines are JSON and keep the path, never the query.
    output = _run_server_and_search(migrated_database_url, tmp_path / "server.log", access_log=True)
    assert SEARCH_TERM not in output
    access = [record for record in _json_records(output) if record["logger"] == "uvicorn.access"]
    assert any('"GET /api/catalog/products HTTP/1.1" 200' in str(record["message"]) for record in access), output
