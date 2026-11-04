import json
import logging

from app.logging_setup import REDACTED, JsonFormatter, redact_text, redact_value


def format_record(message: str, **extra: object) -> dict[str, object]:
    record = logging.makeLogRecord({"name": "test", "levelname": "INFO", "msg": message, **extra})
    return json.loads(JsonFormatter().format(record))


def test_redacts_email_in_message() -> None:
    entry = format_record("payer buyer@example.com approved")
    assert "buyer@example.com" not in entry["message"]
    assert REDACTED in entry["message"]


def test_redacts_connection_string_credentials() -> None:
    text = redact_text("connect postgresql+psycopg://atlas:s3cr3t@db.example.com/atlas failed")
    assert "s3cr3t" not in text
    assert "atlas:" not in text
    assert "db.example.com/atlas" in text


def test_redacts_bearer_tokens() -> None:
    assert "abc.def" not in redact_text("Authorization: Bearer abc.def")


def test_redacts_sensitive_extra_keys() -> None:
    entry = format_record("calling provider", api_key="AIza-secret", client_secret="x", order_ref="ATL-1001")
    assert entry["api_key"] == REDACTED
    assert entry["client_secret"] == REDACTED
    assert entry["order_ref"] == "ATL-1001"


def test_redacts_nested_values() -> None:
    value = redact_value("payload", {"payer": {"email": "a@b.co", "country": "DE"}, "note": "x@y.io"})
    assert value == {"payer": {"email": REDACTED, "country": "DE"}, "note": REDACTED}


def test_output_is_single_line_json() -> None:
    record = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "line one\nline two"})
    output = JsonFormatter().format(record)
    assert "\n" not in output
    assert json.loads(output)["message"] == "line one\nline two"
