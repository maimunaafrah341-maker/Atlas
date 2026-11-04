from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.main import create_app
from app.migrations import head_revision
from tests.conftest import make_settings


def test_liveness(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_migrated_database(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert body["version"] == "dev"
    database = body["database"]
    assert database["status"] == "ok"
    assert database["schema_up_to_date"] is True
    assert database["migration_revision"] == head_revision()
    assert database["dialect"] in {"sqlite", "postgresql"}


def test_readiness_is_degraded_when_schema_not_migrated(database_url: str) -> None:
    with TestClient(create_app(make_settings(database_url))) as client:
        response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["status"] == "ok"
    assert body["database"]["schema_up_to_date"] is False
    assert body["database"]["migration_revision"] is None


def test_unreachable_database_degrades_readiness_but_not_liveness(tmp_path: Path) -> None:
    app = create_app(make_settings(f"sqlite:///{(tmp_path / 'ok.db').as_posix()}"))
    # A database path inside a regular file can never be opened, so every connect fails.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    app.state.engine.dispose()
    app.state.engine = create_engine(f"sqlite:///{(blocker / 'db.sqlite').as_posix()}")

    with TestClient(app) as client:
        liveness = client.get("/healthz")
        readiness = client.get("/api/health")

    assert liveness.status_code == 200
    assert readiness.status_code == 503
    body = readiness.json()
    assert body["status"] == "degraded"
    assert body["database"]["status"] == "error"
    assert "not-a-directory" not in readiness.text  # no connection details leak


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_unknown_api_route_is_json_404(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
