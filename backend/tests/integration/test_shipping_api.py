import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.domain.shipping.models import ShippingRate


@pytest.mark.parametrize(
    ("country", "zone", "amount"),
    [
        ("FR", "europe", 2000),
        ("fr", "europe", 2000),
        (" de ", "europe", 2000),
        ("US", "north_america", 2200),
        ("BR", "south_america", 2800),
        ("JP", "asia_pacific", 1500),
    ],
)
def test_supported_destinations(seeded_client: TestClient, country: str, zone: str, amount: int) -> None:
    response = seeded_client.get("/api/shipping/quote", params={"country": country})
    assert response.status_code == 200
    body = response.json()
    assert body["country"] == country.strip().upper()
    assert body["zone"] == zone
    assert body["price"] == {"amount_minor": amount, "currency": "USD"}
    assert type(body["price"]["amount_minor"]) is int


@pytest.mark.parametrize("country", ["IN", "ZZ", "CN"])
def test_unsupported_destination_is_typed_error(seeded_client: TestClient, country: str) -> None:
    response = seeded_client.get("/api/shipping/quote", params={"country": country})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unsupported_destination"
    assert "price" not in response.text  # never an invented or free price


@pytest.mark.parametrize("country", ["FRA", "1", "F", "F1"])
def test_malformed_country_code(seeded_client: TestClient, country: str) -> None:
    response = seeded_client.get("/api/shipping/quote", params={"country": country})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_country_code"


def test_missing_country_parameter(seeded_client: TestClient) -> None:
    response = seeded_client.get("/api/shipping/quote")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_inactive_zone_is_unsupported(seeded_client: TestClient, seeded_session: Session) -> None:
    seeded_session.execute(update(ShippingRate).where(ShippingRate.zone_code == "europe").values(status="inactive"))
    seeded_session.commit()
    response = seeded_client.get("/api/shipping/quote", params={"country": "FR"})
    assert response.json()["error"]["code"] == "unsupported_destination"
