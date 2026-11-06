import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import InvalidRequest, error_body


def test_error_body_shape() -> None:
    assert error_body("not_found", "Nope") == {"error": {"code": "not_found", "message": "Nope", "details": []}}


def test_invalid_request_carries_field_details() -> None:
    error = InvalidRequest("page_size", "must be between 1 and 50")
    assert error.status_code == 422
    assert error.details == [{"field": "page_size", "issue": "must be between 1 and 50"}]


def test_store_currency_defaults_to_usd() -> None:
    assert Settings(_env_file=None).store_currency == "USD"  # type: ignore[call-arg]


def test_store_currency_other_than_usd_is_refused() -> None:
    with pytest.raises(ValidationError, match="STORE_CURRENCY must be USD"):
        Settings(_env_file=None, store_currency="EUR")  # type: ignore[call-arg]
