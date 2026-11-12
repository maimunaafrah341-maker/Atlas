"""Natural-language product discovery (M5 MVP, ADR-028).

``POST`` so the shopper's words travel in the body, never in a URL or an access log. Read-only:
it changes no cart, reservation, order or payment. Same-origin only, so other sites cannot spend
the Gemini quota through a visitor's browser.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.api.deps import SessionDep
from app.api.security import SameOrigin
from app.domain.catalog.schemas import ProductPage
from app.domain.catalog.vocab import DEFAULT_LANGUAGE, Category, Colour, Language, Size, Tag
from app.domain.discovery.intent import MAX_QUERY_CHARS
from app.domain.discovery.llm import StructuredLLM
from app.domain.discovery.service import discover

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


class DiscoveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=MAX_QUERY_CHARS)]
    lang: Language = DEFAULT_LANGUAGE


class IntentOut(BaseModel):
    categories: list[Category]
    sizes: list[Size]
    colours: list[Colour]
    tags: list[Tag]
    keywords: list[str]


class DiscoveryOut(BaseModel):
    # "gemini": the model's intent, validated. "keywords": the deterministic parser.
    source: Literal["gemini", "groq", "keywords"]
    model: str | None = Field(description="The model id, when a model produced the intent")
    fallback_reason: str | None
    intent: IntentOut
    relaxed: list[Literal["keywords", "tags"]]
    results: ProductPage


@router.post("", response_model=DiscoveryOut, dependencies=[SameOrigin])
def post_discovery(body: DiscoveryIn, session: SessionDep, request: Request, response: Response) -> DiscoveryOut:
    llms: tuple[StructuredLLM, ...] = request.app.state.llms
    found = discover(session, body.query, lang=body.lang, llms=llms)
    response.headers["Cache-Control"] = "no-store"
    intent = found.intent
    return DiscoveryOut(
        source=found.source,
        model=found.model,
        fallback_reason=found.fallback_reason,
        intent=IntentOut(
            categories=list(intent.categories),
            sizes=list(intent.sizes),
            colours=list(intent.colours),
            tags=list(intent.tags),
            keywords=list(intent.keywords),
        ),
        relaxed=list(found.relaxed),
        results=found.results,
    )
