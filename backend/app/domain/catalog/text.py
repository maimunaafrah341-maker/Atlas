"""Portable text normalisation for catalogue search (ADR-021).

The same normalisation is applied to stored product text and to queries, and matching is
done with plain ``LIKE`` on the normalised text. SQLite and PostgreSQL therefore return
identical results. Known limitation: no stemming. Matching is by word prefix, so "jacket"
matches "jackets", but "chaquetas" does not match "chaqueta".
"""

from __future__ import annotations

import re
import unicodedata

MIN_TERM_LENGTH = 2
MAX_QUERY_TERMS = 8

_NON_WORD = re.compile(r"[\W_]+")


def normalize(text: str) -> str:
    """Case-fold, strip accents and collapse everything but letters and digits to single spaces."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(_NON_WORD.sub(" ", stripped).split())


def search_document(*parts: str) -> str:
    """Stored search text, padded with spaces so ``LIKE '% term%'`` matches at word starts."""
    return f" {normalize(' '.join(parts))} "


def query_terms(query: str) -> list[str]:
    """Distinct normalised terms of at least MIN_TERM_LENGTH characters, at most MAX_QUERY_TERMS.

    Terms contain only letters and digits, so they are safe inside a LIKE pattern
    without escaping.
    """
    terms: list[str] = []
    for term in normalize(query).split():
        if len(term) >= MIN_TERM_LENGTH and term not in terms:
            terms.append(term)
    return terms[:MAX_QUERY_TERMS]
