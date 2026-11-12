"""A scripted stand-in for the Gemini client. Tests and CI never call Google."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.domain.discovery.llm import LLMUnavailable

MODEL = "gemini-3.8-flash"


def intent_json(
    *,
    categories: list[str] | None = None,
    sizes: list[str] | None = None,
    colours: list[str] | None = None,
    tags: list[str] | None = None,
    keywords: list[str] | None = None,
    **extra: Any,
) -> str:
    return json.dumps(
        {
            "categories": categories or [],
            "sizes": sizes or [],
            "colours": colours or [],
            "tags": tags or [],
            "keywords": keywords or [],
            **extra,
        }
    )


@dataclass
class FakeLLM:
    """Replies with the scripted strings or raises the scripted exceptions, in order."""

    replies: list[str | Exception] = field(default_factory=list)
    provider: str = "gemini"
    model: str = MODEL
    prompts: list[str] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)

    def generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> str:
        self.prompts.append(user)
        self.schemas.append(schema)
        if not self.replies:
            raise LLMUnavailable("no scripted reply")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
