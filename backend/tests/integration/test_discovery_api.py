"""POST /api/discovery with a fake Gemini (M5). SQLite and PostgreSQL. Nothing reaches Google.

Proves: a valid intent drives the ordinary catalogue search and every product shown comes from
it; invalid output, timeouts and API errors fall back to the keyword parser; model output can
neither inject products, prices, stock, shipping or payment data nor add search text; the
endpoint is same-origin, read-only, cookie-free and keeps the shopper's words out of logs.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.cart.models import Cart
from app.domain.catalog.service import SearchFilters, search_products
from app.domain.discovery.llm import LLMUnavailable
from app.domain.reservations.models import Reservation
from app.logging_setup import JsonFormatter
from app.main import create_app
from tests.conftest import make_settings, open_session
from tests.fake_llm import MODEL, FakeLLM, intent_json

ORIGIN = "http://testserver"
ASK = "I'm looking for a lightweight navy jacket in medium."
NAVY_MEDIUM_JACKET = intent_json(categories=["jackets"], sizes=["M"], colours=["navy"], keywords=["lightweight"])


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def api(seeded_url: str, llm: FakeLLM) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(seeded_url), llms=[llm])) as test_client:
        yield test_client


@pytest.fixture
def session(seeded_url: str) -> Iterator[Session]:
    yield from open_session(seeded_url)


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def ask(client: TestClient, query: str = ASK, **extra: Any) -> Any:
    response = client.post("/api/discovery", json={"query": query, **extra}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()


def catalogue(session: Session, **filters: Any) -> dict[str, Any]:
    """What the ordinary catalogue search returns for these filters, as JSON."""
    return search_products(session, SearchFilters(page_size=12, **filters)).model_dump(mode="json")


def test_a_valid_gemini_intent_drives_the_catalogue_search(api: TestClient, llm: FakeLLM, session: Session) -> None:
    llm.replies.append(NAVY_MEDIUM_JACKET)
    body = ask(api)
    assert body["source"] == "gemini"
    assert body["model"] == MODEL
    assert body["fallback_reason"] is None
    assert body["intent"] == {
        "categories": ["jackets"],
        "sizes": ["M"],
        "colours": ["navy"],
        "tags": [],
        "keywords": ["lightweight"],
    }
    assert body["relaxed"] == []
    # Exactly what the catalogue service returns for those filters: products, prices, stock state.
    expected = catalogue(session, query="lightweight", categories=("jackets",), sizes=("M",), colours=("navy",))
    assert body["results"] == expected
    assert body["results"]["total"] >= 1
    assert "ridgeline-rain-shell" in {item["slug"] for item in body["results"]["items"]}
    assert len(llm.prompts) == 1


def test_invalid_output_is_retried_once_then_falls_back_to_keywords(
    api: TestClient, llm: FakeLLM, session: Session
) -> None:
    llm.replies.extend(["not json", intent_json(categories=["parkas"])])
    body = ask(api)
    assert len(llm.prompts) == 2  # bounded: one retry
    assert (body["source"], body["model"], body["fallback_reason"]) == ("keywords", None, "gemini: invalid_output")
    assert body["intent"]["categories"] == ["jackets"]
    assert body["intent"]["colours"] == ["navy"]
    assert body["intent"]["sizes"] == ["M"]
    assert body["results"] == catalogue(session, categories=("jackets",), sizes=("M",), colours=("navy",))
    assert body["results"]["total"] >= 1  # the shopper can still find the jacket


def test_one_invalid_reply_then_a_valid_one_uses_the_model(api: TestClient, llm: FakeLLM) -> None:
    llm.replies.extend(['{"categories": ["jackets"]}', NAVY_MEDIUM_JACKET])  # missing fields, then valid
    body = ask(api)
    assert (body["source"], len(llm.prompts)) == ("gemini", 2)


@pytest.mark.parametrize(
    ("failure", "attempts", "reason"),
    [
        (LLMUnavailable("timeout"), 1, "gemini: unavailable: timeout"),
        (LLMUnavailable("http 400"), 1, "gemini: unavailable: http 400"),
        (LLMUnavailable("http 429", retryable=True), 2, "gemini: unavailable: http 429"),
        (LLMUnavailable("http 503", retryable=True), 2, "gemini: unavailable: http 503"),
        (LLMUnavailable("blocked"), 1, "gemini: unavailable: blocked"),
    ],
)
def test_an_unavailable_model_falls_back_to_keywords(
    api: TestClient, llm: FakeLLM, failure: LLMUnavailable, attempts: int, reason: str
) -> None:
    llm.replies.extend([failure, failure])
    body = ask(api)
    assert len(llm.prompts) == attempts
    assert (body["source"], body["fallback_reason"]) == ("keywords", reason)
    assert body["results"]["total"] >= 1


def test_a_transient_error_then_a_valid_reply_uses_the_model(api: TestClient, llm: FakeLLM) -> None:
    llm.replies.extend([LLMUnavailable("http 503", retryable=True), NAVY_MEDIUM_JACKET])
    assert ask(api)["source"] == "gemini"


def test_without_gemini_configured_discovery_uses_the_keyword_parser(seeded_url: str, session: Session) -> None:
    with TestClient(create_app(make_settings(seeded_url))) as client:
        body = ask(client)
    assert (body["source"], body["model"], body["fallback_reason"]) == ("keywords", None, "not_configured")
    assert body["results"] == catalogue(session, categories=("jackets",), sizes=("M",), colours=("navy",))


@pytest.mark.parametrize(
    "injected",
    [
        {"product_ids": [1]},
        {"slug": "summit-3l-shell"},
        {"price_minor": 1},
        {"stock": 1000},
        {"shipping_minor": 0},
        {"discount_percent": 100},
        {"payment": {"status": "COMPLETED"}},
    ],
)
def test_model_output_cannot_inject_products_prices_stock_shipping_or_payment(
    api: TestClient, llm: FakeLLM, session: Session, injected: dict[str, Any]
) -> None:
    poisoned = intent_json(categories=["jackets"], colours=["navy"], sizes=["M"], **injected)
    llm.replies.extend([poisoned, poisoned])
    body = ask(api)
    assert (body["source"], body["fallback_reason"]) == ("keywords", "gemini: invalid_output")
    # The products are the catalogue's answer to the shopper's own words, nothing the model named.
    assert body["results"] == catalogue(session, categories=("jackets",), sizes=("M",), colours=("navy",))
    for item in body["results"]["items"]:
        for variant in item["variants"]:
            assert variant["price"]["amount_minor"] > 1
    assert set(body) == {"source", "model", "fallback_reason", "intent", "relaxed", "results"}


def test_model_keywords_the_shopper_did_not_write_never_reach_the_search(
    api: TestClient, llm: FakeLLM, session: Session
) -> None:
    llm.replies.append(intent_json(categories=["jackets"], keywords=["ember", "insulated", "lightweight"]))
    body = ask(api)
    assert body["intent"]["keywords"] == ["lightweight"]  # "ember", "insulated" were not in the request
    assert body["results"] == catalogue(session, query="lightweight", categories=("jackets",))


def test_a_request_with_no_match_relaxes_keywords_then_tags(api: TestClient, llm: FakeLLM, session: Session) -> None:
    query = "zzzz socks for winter"
    llm.replies.append(intent_json(categories=["socks"], tags=["winter", "waterproof"], keywords=["zzzz"]))
    body = ask(api, query)
    assert body["source"] == "gemini"
    assert body["relaxed"][0] == "keywords"
    assert body["results"]["total"] >= 1
    assert all(item["category"] == "socks" for item in body["results"]["items"])


def test_products_are_localised_by_the_requested_language(api: TestClient, llm: FakeLLM, session: Session) -> None:
    llm.replies.append(intent_json(categories=["jackets"], colours=["navy"]))
    body = ask(api, "chaqueta azul marino", lang="es")
    assert body["results"] == catalogue(session, lang="es", categories=("jackets",), colours=("navy",))
    assert {item["lang"] for item in body["results"]["items"]} <= {"es", "en"}


def test_the_model_sees_only_the_redacted_request(api: TestClient, llm: FakeLLM) -> None:
    llm.replies.append(NAVY_MEDIUM_JACKET)
    ask(api, "navy jacket medium, reach me at jane.doe@example.com or +44 20 7946 0958")
    (prompt,) = llm.prompts
    assert "navy jacket medium" in prompt
    assert "jane.doe" not in prompt
    assert "example.com" not in prompt
    assert "7946" not in prompt


def test_the_shoppers_words_and_model_output_are_never_logged(api: TestClient, llm: FakeLLM, logs: io.StringIO) -> None:
    llm.replies.append(NAVY_MEDIUM_JACKET)
    ask(api, "lightweight navy jacket medium for jane.doe@example.com")
    output = logs.getvalue()
    assert '"discovery"' in output  # the event is logged, with metadata only
    for secret in ("jane.doe", "example.com", "lightweight navy", NAVY_MEDIUM_JACKET):
        assert secret not in output


def test_discovery_is_read_only_and_sets_no_cookie(api: TestClient, llm: FakeLLM, session: Session) -> None:
    llm.replies.append(NAVY_MEDIUM_JACKET)
    response = api.post("/api/discovery", json={"query": ASK}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert session.scalar(select(func.count()).select_from(Cart)) == 0
    assert session.scalar(select(func.count()).select_from(Reservation)) == 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example"},
        {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_discovery_is_same_origin_only(api: TestClient, llm: FakeLLM, headers: dict[str, str]) -> None:
    response = api.post("/api/discovery", json={"query": ASK}, headers=headers)
    assert response.status_code == 403
    assert llm.prompts == []  # no quota spent


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": " a "},
        {"query": "x" * 301},
        {"query": 7},
        {"query": ASK, "lang": "it"},
        {"query": ASK, "model": "gemini-pro"},
        {"query": ASK, "price_minor": 1},
    ],
)
def test_invalid_requests_are_rejected_before_the_model(api: TestClient, llm: FakeLLM, payload: dict[str, Any]) -> None:
    response = api.post("/api/discovery", json=payload, headers={"Origin": ORIGIN})
    assert response.status_code == 422
    assert llm.prompts == []


# --- Groq fallback ---------------------------------------------------------------------------------


@pytest.fixture
def chain(seeded_url: str) -> Iterator[tuple[TestClient, FakeLLM, FakeLLM]]:
    gemini, groq = FakeLLM(), FakeLLM(provider="groq", model="openai/gpt-oss-20b")
    with TestClient(create_app(make_settings(seeded_url), llms=[gemini, groq])) as client:
        yield client, gemini, groq


def test_groq_answers_when_gemini_is_unavailable_without_retrying_gemini(
    chain: tuple[TestClient, FakeLLM, FakeLLM], session: Session
) -> None:
    client, gemini, groq = chain
    gemini.replies.extend([LLMUnavailable("http 503", retryable=True), NAVY_MEDIUM_JACKET])
    groq.replies.append(NAVY_MEDIUM_JACKET)
    body = ask(client)
    assert (body["source"], body["model"]) == ("groq", "openai/gpt-oss-20b")
    assert body["fallback_reason"] == "gemini: unavailable: http 503"
    assert (len(gemini.prompts), len(groq.prompts)) == (1, 1)  # Gemini handed over at once
    expected = catalogue(session, query="lightweight", categories=("jackets",), sizes=("M",), colours=("navy",))
    assert body["results"] == expected


def test_groq_output_is_validated_like_geminis(chain: tuple[TestClient, FakeLLM, FakeLLM]) -> None:
    client, gemini, groq = chain
    gemini.replies.extend([LLMUnavailable("timeout")])
    poisoned = intent_json(categories=["jackets"], price_minor=1)
    groq.replies.extend([poisoned, poisoned])
    body = ask(client)
    assert (body["source"], body["model"]) == ("keywords", None)
    assert body["fallback_reason"] == "gemini: unavailable: timeout; groq: invalid_output"
    assert len(groq.prompts) == 2  # one retry for invalid output
    assert body["results"]["total"] >= 1


def test_gemini_is_preferred_when_it_answers(chain: tuple[TestClient, FakeLLM, FakeLLM]) -> None:
    client, gemini, groq = chain
    gemini.replies.append(NAVY_MEDIUM_JACKET)
    body = ask(client)
    assert (body["source"], body["model"], body["fallback_reason"]) == ("gemini", MODEL, None)
    assert groq.prompts == []


def test_when_every_model_fails_search_still_works(chain: tuple[TestClient, FakeLLM, FakeLLM]) -> None:
    client, gemini, groq = chain
    gemini.replies.append(LLMUnavailable("timeout"))
    groq.replies.extend([LLMUnavailable("http 503", retryable=True)] * 2)
    body = ask(client)
    assert body["source"] == "keywords"
    assert body["fallback_reason"] == "gemini: unavailable: timeout; groq: unavailable: http 503"
    assert len(groq.prompts) == 2  # the last model retries a transient error once
    assert body["intent"]["categories"] == ["jackets"]
