from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings

INDEX = '<!doctype html><html><body><div id="root"></div></body></html>'


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('atlas')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside dist", encoding="utf-8")
    return root


@pytest.fixture
def spa_client(migrated_database_url: str, dist: Path) -> Iterator[TestClient]:
    app = create_app(make_settings(migrated_database_url, frontend_dist_dir=dist))
    with TestClient(app) as client:
        yield client


def test_root_serves_index(spa_client: TestClient) -> None:
    response = spa_client.get("/")
    assert response.status_code == 200
    assert '<div id="root">' in response.text


def test_client_routes_fall_back_to_index(spa_client: TestClient) -> None:
    response = spa_client.get("/merchant/orders")
    assert response.status_code == 200
    assert '<div id="root">' in response.text


def test_assets_and_root_files_are_served(spa_client: TestClient) -> None:
    assert spa_client.get("/assets/app.js").text == "console.log('atlas')"
    assert spa_client.get("/favicon.svg").text == "<svg/>"


def test_api_paths_never_fall_back_to_index(spa_client: TestClient) -> None:
    response = spa_client.get("/api/unknown")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_health_still_served_with_spa(spa_client: TestClient) -> None:
    assert spa_client.get("/api/health").json()["status"] == "ok"


def test_no_path_traversal_outside_dist(spa_client: TestClient) -> None:
    response = spa_client.get("/..%2Fsecret.txt")
    assert "outside dist" not in response.text


def test_missing_build_serves_api_only(migrated_database_url: str, tmp_path: Path) -> None:
    app = create_app(make_settings(migrated_database_url, frontend_dist_dir=tmp_path / "missing"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/healthz").status_code == 200
