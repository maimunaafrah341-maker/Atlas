"""The LLM provider protocol and the Gemini adapter (ADR-005).

Only this module knows a vendor. It speaks the Gemini REST API (``generateContent``) over the
project's existing ``httpx`` dependency, so no vendor SDK is installed. The API key travels only
in the ``x-goog-api-key`` header: it is never part of a URL, an error message or a log record.
Tests and CI use a fake provider and never reach Google.
"""

from __future__ import annotations

import re
from typing import Any, Final, Protocol

import httpx

from app.domain.catalog.vocab import CATEGORIES, COLOURS, SIZES, TAGS
from app.domain.discovery.intent import (
    FIELDS,
    MAX_CATEGORIES,
    MAX_COLOURS,
    MAX_KEYWORDS,
    MAX_SIZES,
    MAX_TAGS,
)

GEMINI_API: Final = "https://generativelanguage.googleapis.com/v1beta"
MODEL_PATTERN: Final = re.compile(r"gemini-[a-z0-9][a-z0-9.-]{0,62}")  # goes into the URL path
# Finite and short: discovery must answer quickly, with the keyword fallback if Gemini is slow.
TIMEOUT: Final = httpx.Timeout(10.0, connect=4.0)
MAX_REPLY_BYTES: Final = 64 * 1024


class LLMUnavailable(Exception):
    """The provider could not answer: timeout, network error, quota, non-2xx, blocked or empty."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class StructuredLLM(Protocol):
    provider: str
    model: str

    def generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> str:
        """The model's raw JSON text for ``user``, constrained to ``schema``. Validated by the caller."""
        ...


def _enum_array(values: tuple[str, ...], max_items: int) -> dict[str, Any]:
    return {"type": "ARRAY", "items": {"type": "STRING", "enum": list(values)}, "maxItems": max_items}


def intent_schema() -> dict[str, Any]:
    """The Gemini response schema for a ShoppingIntent. The backend re-validates every field."""
    return {
        "type": "OBJECT",
        "properties": {
            "categories": _enum_array(CATEGORIES, MAX_CATEGORIES),
            "sizes": _enum_array(SIZES, MAX_SIZES),
            "colours": _enum_array(COLOURS, MAX_COLOURS),
            "tags": _enum_array(TAGS, MAX_TAGS),
            "keywords": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": MAX_KEYWORDS},
        },
        "required": list(FIELDS),
        "propertyOrdering": list(FIELDS),
    }


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key: str, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not api_key:
            raise ValueError("a Gemini API key is required")
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("LLM_MODEL is not a Gemini model id")
        self.model = model
        self._http = httpx.Client(
            base_url=GEMINI_API,
            timeout=TIMEOUT,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
        }
        try:
            response = self._http.post(f"/models/{self.model}:generateContent", json=body)
        except httpx.TimeoutException:
            raise LLMUnavailable("timeout") from None
        except httpx.HTTPError as error:
            raise LLMUnavailable(f"network: {type(error).__name__}", retryable=True) from None
        if response.status_code != 200:
            # Status only: an error body may echo the request.
            retryable = response.status_code == 429 or response.status_code >= 500
            raise LLMUnavailable(f"http {response.status_code}", retryable=retryable)
        if len(response.content) > MAX_REPLY_BYTES:
            raise LLMUnavailable("reply too large")
        try:
            data = response.json()
        except ValueError:
            raise LLMUnavailable("reply is not JSON") from None
        return _reply_text(data)


def _reply_text(data: Any) -> str:
    if not isinstance(data, dict):
        raise LLMUnavailable("reply is not an object")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        blocked = isinstance(data.get("promptFeedback"), dict) and data["promptFeedback"].get("blockReason")
        raise LLMUnavailable("blocked" if blocked else "no candidates")
    candidate = candidates[0]
    finish = candidate.get("finishReason")
    if finish not in (None, "STOP"):
        raise LLMUnavailable(f"finish {str(finish)[:32]}")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise LLMUnavailable("no content")
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
    )
    if not text.strip():
        raise LLMUnavailable("empty reply")
    return text
