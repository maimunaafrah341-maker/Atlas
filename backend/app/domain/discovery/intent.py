"""The shopping intent: what a shopper asked for, in closed catalogue codes only.

Two sources produce it. ``parse_model_output`` validates a model's JSON strictly: every field
present, nothing else, every value from the catalogue vocabularies. ``keyword_intent`` is the
deterministic fallback. Either way the intent can only *filter* the catalogue: it has no field
for a product, a price, stock, shipping or anything else the backend decides.
"""

from __future__ import annotations

import re
from typing import Final, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.catalog.text import MIN_TERM_LENGTH, normalize
from app.domain.catalog.vocab import COLOURS, Category, Colour, Size, Tag

MAX_QUERY_CHARS: Final = 300
MAX_CATEGORIES: Final = 2
MAX_SIZES: Final = 3
MAX_COLOURS: Final = 3
MAX_TAGS: Final = 4
MAX_KEYWORDS: Final = 4
MAX_KEYWORD_LENGTH: Final = 24
FIELDS: Final = ("categories", "sizes", "colours", "tags", "keywords")

_T = TypeVar("_T")
_KEYWORD = re.compile(rf"[a-z0-9]{{{MIN_TERM_LENGTH},{MAX_KEYWORD_LENGTH}}}")


class InvalidIntent(ValueError):
    """The model's output is not a valid shopping intent. The message never contains the output."""


class ShoppingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: tuple[Category, ...] = Field(max_length=MAX_CATEGORIES)
    sizes: tuple[Size, ...] = Field(max_length=MAX_SIZES)
    colours: tuple[Colour, ...] = Field(max_length=MAX_COLOURS)
    tags: tuple[Tag, ...] = Field(max_length=MAX_TAGS)
    keywords: tuple[str, ...] = Field(max_length=MAX_KEYWORDS)

    @property
    def is_empty(self) -> bool:
        return not (self.categories or self.sizes or self.colours or self.tags or self.keywords)


EMPTY_INTENT: Final = ShoppingIntent(categories=(), sizes=(), colours=(), tags=(), keywords=())


def _unique(values: tuple[_T, ...]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys(values))


def _words(text: str) -> list[str]:
    return normalize(text).split()


def parse_model_output(raw: str, shopper_text: str) -> ShoppingIntent:
    """Validate a model's JSON reply. Raises ``InvalidIntent`` for anything but a clean intent.

    Strict: the JSON must be one object with exactly the five fields, each a list of known codes.
    An unknown category, size, colour or tag, a missing field, or any extra field (a product id,
    a price, ...) rejects the whole reply. Keywords are filtered instead of rejected: only single
    words that the shopper actually wrote survive, so the model cannot add search text of its own.
    """
    try:
        intent = ShoppingIntent.model_validate_json(raw, strict=True)
    except ValidationError:
        raise InvalidIntent("the model's reply is not a valid shopping intent") from None
    written = set(_words(shopper_text))
    keywords: list[str] = []
    for keyword in intent.keywords:
        term = normalize(keyword)
        if _KEYWORD.fullmatch(term) and term in written and term not in keywords:
            keywords.append(term)
    return ShoppingIntent(
        categories=_unique(intent.categories),
        sizes=_unique(intent.sizes),
        colours=_unique(intent.colours),
        tags=_unique(intent.tags),
        keywords=tuple(keywords),
    )


# --- Deterministic fallback -------------------------------------------------------------------------

_CATEGORY_WORDS: Final[dict[str, Category]] = {
    **dict.fromkeys(
        ("jacket", "jackets", "shell", "shells", "coat", "coats", "parka", "anorak", "windbreaker"), "jackets"
    ),
    **dict.fromkeys(("baselayer", "baselayers", "thermal", "thermals"), "base_layers"),
    **dict.fromkeys(("trouser", "trousers", "pants", "pant", "leggings"), "trousers"),
    **dict.fromkeys(("sock", "socks"), "socks"),
    **dict.fromkeys(
        ("accessory", "accessories", "hat", "hats", "beanie", "beanies", "cap", "caps", "glove", "gloves", "gaiter"),
        "accessories",
    ),
}
_CATEGORY_PHRASES: Final[dict[str, Category]] = {"base layer": "base_layers", "base layers": "base_layers"}
_COLOUR_WORDS: Final[dict[str, Colour]] = {
    **{colour: colour for colour in COLOURS},  # type: ignore[misc]
    "gray": "grey",
}
_SIZE_WORDS: Final[dict[str, Size]] = {
    "xs": "XS",
    "xl": "XL",
    "xxl": "XXL",
    "small": "S",
    "medium": "M",
    "large": "L",
}
_SIZE_PHRASES: Final[dict[str, Size]] = {
    "extra small": "XS",
    "extra large": "XL",
    "x large": "XL",
    "one size": "ONE_SIZE",
    "size s": "S",
    "size m": "M",
    "size l": "L",
}
_TAG_WORDS: Final[dict[str, Tag]] = {
    "waterproof": "waterproof",
    "rain": "waterproof",
    "windproof": "windproof",
    "insulated": "insulated",
    "packable": "packable",
    "breathable": "breathable",
    "merino": "merino",
    "uv": "uv_protection",
    "hiking": "hiking",
    "hike": "hiking",
    "trekking": "trekking",
    "trek": "trekking",
    "running": "trail_running",
    "camping": "camping",
    "travel": "travel",
    "winter": "winter",
}
_TAG_PHRASES: Final[dict[str, Tag]] = {"quick dry": "quick_dry", "trail running": "trail_running"}


def keyword_intent(text: str) -> ShoppingIntent:
    """The deterministic intent for ``text``: known words and phrases mapped to catalogue codes.

    When nothing is recognised, up to four of the shopper's words become keywords, so the
    request still runs as an ordinary catalogue text search.
    """
    words = _words(text[:MAX_QUERY_CHARS])
    # Phrases first; each match consumes its words, so "extra large" is XL and never also L.
    rest = f" {' '.join(words)} "
    found: dict[str, list[str]] = {"categories": [], "sizes": [], "tags": []}
    for name, phrases in (("categories", _CATEGORY_PHRASES), ("sizes", _SIZE_PHRASES), ("tags", _TAG_PHRASES)):
        for phrase, code in phrases.items():
            if f" {phrase} " in rest:
                found[name].append(code)
                rest = rest.replace(f" {phrase} ", " | ")
    remaining = rest.split()
    categories = found["categories"] + [_CATEGORY_WORDS[w] for w in remaining if w in _CATEGORY_WORDS]
    sizes = found["sizes"] + [_SIZE_WORDS[w] for w in remaining if w in _SIZE_WORDS]
    colours = [_COLOUR_WORDS[w] for w in remaining if w in _COLOUR_WORDS]
    tags = found["tags"] + [_TAG_WORDS[w] for w in remaining if w in _TAG_WORDS]
    intent = ShoppingIntent(
        categories=_unique(tuple(categories))[:MAX_CATEGORIES],
        sizes=_unique(tuple(sizes))[:MAX_SIZES],
        colours=_unique(tuple(colours))[:MAX_COLOURS],
        tags=_unique(tuple(tags))[:MAX_TAGS],
        keywords=(),
    )
    if not intent.is_empty:
        return intent
    keywords = [w for w in dict.fromkeys(words) if _KEYWORD.fullmatch(w)][:MAX_KEYWORDS]
    return intent.model_copy(update={"keywords": tuple(keywords)})


# --- What the model may see -------------------------------------------------------------------------

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_LONG_NUMBER = re.compile(r"\+?\d(?:[\s().-]*\d){5,}")


def redact(text: str) -> str:
    """The shopper's text as sent to the model: trimmed, without email addresses or long numbers.

    A product request never needs them, and they could be contact or card details.
    """
    text = text[:MAX_QUERY_CHARS]
    text = _EMAIL.sub("[email]", text)
    return _LONG_NUMBER.sub("[number]", text)
