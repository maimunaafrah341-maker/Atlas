"""The Gemini adapter against a mocked transport (M5). Nothing here reaches Google."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.domain.discovery.llm import GeminiClient, LLMUnavailable, intent_schema

KEY = "test-gemini-key-value-0123456789"
SYSTEM = "system prompt"
USER = "Shopper request: navy jacket"


def client(handler: Callable[[httpx.Request], httpx.Response], model: str = "gemini-3.8-flash") -> GeminiClient:
    return GeminiClient(KEY, model, transport=httpx.MockTransport(handler))


def reply(text: str, **candidate: Any) -> httpx.Response:
    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP", **candidate}]}
    )


def call(gemini: GeminiClient) -> str:
    return gemini.generate_json(system=SYSTEM, user=USER, schema=intent_schema())


def test_the_request_is_a_constrained_generate_content_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply('{"categories": []}')

    assert call(client(handler)) == '{"categories": []}'
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    )
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    body = json.loads(request.content)
    assert KEY not in request.content.decode()
    assert body["systemInstruction"] == {"parts": [{"text": SYSTEM}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": USER}]}]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == intent_schema()


def test_the_schema_offers_only_catalogue_codes() -> None:
    schema = intent_schema()
    assert (
        set(schema["properties"]) == set(schema["required"]) == {"categories", "sizes", "colours", "tags", "keywords"}
    )
    assert "navy" in schema["properties"]["colours"]["items"]["enum"]
    assert "jackets" in schema["properties"]["categories"]["items"]["enum"]
    for name in ("categories", "sizes", "colours", "tags"):
        assert "enum" in schema["properties"][name]["items"]


def test_thought_parts_are_skipped_and_text_parts_joined() -> None:
    parts = [{"text": "thinking...", "thought": True}, {"text": '{"a":'}, {"text": " 1}"}]
    response = httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})
    assert call(client(lambda _: response)) == '{"a": 1}'


@pytest.mark.parametrize(
    ("response", "reason", "retryable"),
    [
        (httpx.Response(400, json={"error": {"message": f"bad key {KEY}"}}), "http 400", False),
        (httpx.Response(401), "http 401", False),
        (httpx.Response(403), "http 403", False),
        (httpx.Response(404), "http 404", False),
        (httpx.Response(429), "http 429", True),
        (httpx.Response(500), "http 500", True),
        (httpx.Response(503), "http 503", True),
        (httpx.Response(200, text="<html>"), "reply is not JSON", False),
        (httpx.Response(200, json=[]), "reply is not an object", False),
        (httpx.Response(200, json={}), "no candidates", False),
        (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "blocked", False),
        (reply("{}", finishReason="MAX_TOKENS"), "finish MAX_TOKENS", False),
        (reply("{}", finishReason="SAFETY"), "finish SAFETY", False),
        (reply("   "), "empty reply", False),
        (httpx.Response(200, json={"candidates": [{"finishReason": "STOP"}]}), "no content", False),
        (httpx.Response(200, content=b'{"x":"' + b"a" * 70_000 + b'"}'), "reply too large", False),
    ],
)
def test_failures_are_unavailable_and_never_echo_the_key(
    response: httpx.Response, reason: str, retryable: bool
) -> None:
    with pytest.raises(LLMUnavailable) as caught:
        call(client(lambda _: response))
    assert caught.value.reason == reason
    assert caught.value.retryable is retryable
    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None


def test_a_timeout_is_unavailable_and_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMUnavailable) as caught:
        call(client(handler))
    assert (caught.value.reason, caught.value.retryable) == ("timeout", False)


def test_a_network_error_is_unavailable_and_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect {request.url}", request=request)

    with pytest.raises(LLMUnavailable) as caught:
        call(client(handler))
    assert (caught.value.reason, caught.value.retryable) == ("network: ConnectError", True)


def test_redirects_are_not_followed() -> None:
    with pytest.raises(LLMUnavailable, match="http 302"):
        call(client(lambda _: httpx.Response(302, headers={"Location": "https://example.com/"})))


@pytest.mark.parametrize("model", ["", "gpt-4o", "gemini-3.8-flash/../x", "gemini-3.8-flash?key=1", "Gemini-3.8"])
def test_only_gemini_model_ids_are_accepted(model: str) -> None:
    with pytest.raises(ValueError, match="model"):
        GeminiClient(KEY, model)


def test_a_key_is_required() -> None:
    with pytest.raises(ValueError, match="key"):
        GeminiClient("", "gemini-3.8-flash")


# --- settings ---------------------------------------------------------------------------------------


def settings(**values: str) -> Settings:
    return Settings(_env_file=None, app_env="test", database_url="sqlite://", **values)  # type: ignore[call-arg, arg-type]


def test_discovery_needs_the_provider_and_a_key() -> None:
    assert settings().llm_problem == "LLM_PROVIDER is not set"
    assert settings(llm_provider="gemini").llm_problem == "GEMINI_API_KEY is required"
    assert settings(llm_provider="openai", gemini_api_key=KEY).llm_problem is not None
    assert settings(llm_provider="Gemini", gemini_api_key=KEY).llm_problem is None


def test_the_model_defaults_to_gemini_3_8_flash_and_must_be_a_gemini_id() -> None:
    assert settings().resolved_llm_model == "gemini-3.8-flash"
    assert settings(llm_model="  ").resolved_llm_model == "gemini-3.8-flash"
    assert settings(llm_model="gemini-flash-latest").resolved_llm_model == "gemini-flash-latest"
    bad = settings(llm_provider="gemini", gemini_api_key=KEY, llm_model="gemini/../../x")
    assert bad.llm_problem is not None
    assert "LLM_MODEL" in bad.llm_problem


def test_the_key_never_appears_in_the_settings_repr_or_problem() -> None:
    configured = settings(llm_provider="gemini", gemini_api_key=KEY, llm_model="nope")
    assert KEY not in repr(configured)
    assert KEY not in (configured.llm_problem or "")
