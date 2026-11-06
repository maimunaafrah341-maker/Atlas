import pytest

from app.domain.catalog.text import MAX_QUERY_TERMS, normalize, query_terms, search_document
from app.domain.catalog.vocab import SIZE_ORDER, SIZES, sql_in


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Chaquéta  IMPERMEABLE!", "chaqueta impermeable"),
        ("Straße", "strasse"),
        ("2,5-Lagen-Jacke", "2 5 lagen jacke"),
        ("Imperméable, léger.", "impermeable leger"),
        ("Jaqueta impermeável", "jaqueta impermeavel"),
        ("snake_case and\ttabs", "snake case and tabs"),
        ("", ""),
    ],
)
def test_normalize(given: str, expected: str) -> None:
    assert normalize(given) == expected


def test_search_document_is_padded_for_word_prefix_matching() -> None:
    assert search_document("Rain Shell", "Light jacket") == " rain shell light jacket "


def test_query_terms_drop_short_and_duplicate_words() -> None:
    assert query_terms("a Rain rain shell M") == ["rain", "shell"]


def test_query_terms_are_capped() -> None:
    words = " ".join(f"word{i}" for i in range(20))
    assert len(query_terms(words)) == MAX_QUERY_TERMS


def test_query_terms_are_like_safe() -> None:
    for term in query_terms("100% wool_blend jacket's"):
        assert "%" not in term
        assert "_" not in term
        assert "'" not in term


def test_sizes_have_display_order() -> None:
    assert [SIZE_ORDER[size] for size in ("XS", "S", "M", "L", "XL")] == sorted(
        SIZE_ORDER[size] for size in ("XS", "S", "M", "L", "XL")
    )
    assert set(SIZE_ORDER) == set(SIZES)


def test_sql_in_quotes_values() -> None:
    assert sql_in("lang", ("en", "pt-BR")) == "lang IN ('en', 'pt-BR')"
    assert sql_in("x", ("it's",)) == "x IN ('it''s')"
