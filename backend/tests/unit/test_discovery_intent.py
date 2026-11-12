"""ShoppingIntent validation, the keyword fallback and redaction (M5). No network, no database."""

from __future__ import annotations

import json

import pytest

from app.domain.discovery.intent import (
    MAX_QUERY_CHARS,
    InvalidIntent,
    ShoppingIntent,
    keyword_intent,
    parse_model_output,
    redact,
)
from tests.fake_llm import intent_json

ASK = "I'm looking for a lightweight navy jacket in medium."


def intent(**fields: tuple[str, ...]) -> ShoppingIntent:
    values: dict[str, tuple[str, ...]] = {"categories": (), "sizes": (), "colours": (), "tags": (), "keywords": ()}
    values.update(fields)
    return ShoppingIntent.model_validate(values)


def test_a_valid_model_reply_becomes_the_intent() -> None:
    raw = intent_json(categories=["jackets"], sizes=["M"], colours=["navy"], keywords=["lightweight"])
    assert parse_model_output(raw, ASK) == intent(
        categories=("jackets",), sizes=("M",), colours=("navy",), keywords=("lightweight",)
    )


def test_duplicates_are_collapsed() -> None:
    raw = intent_json(categories=["jackets", "jackets"], colours=["navy", "navy"], keywords=["lightweight"] * 2)
    assert parse_model_output(raw, ASK) == intent(categories=("jackets",), colours=("navy",), keywords=("lightweight",))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "{",
        "[]",
        "null",
        '"jackets"',
        "```json\n{}\n```",
        json.dumps({"categories": [], "sizes": [], "colours": [], "tags": []}),  # keywords missing
        json.dumps({"categories": ["jackets"]}),  # most fields missing
        intent_json(categories=["parkas"]),  # unknown category
        intent_json(sizes=["XXXL"]),  # unknown size
        intent_json(sizes=["m"]),  # codes are case-sensitive
        intent_json(colours=["purple"]),  # unknown colour
        intent_json(tags=["fireproof"]),  # unknown tag
        json.dumps({"categories": "jackets", "sizes": [], "colours": [], "tags": [], "keywords": []}),  # not a list
        json.dumps({"categories": [1], "sizes": [], "colours": [], "tags": [], "keywords": []}),  # not a string
        json.dumps({"categories": [], "sizes": [], "colours": [], "tags": [], "keywords": [7]}),
        intent_json(categories=["jackets", "socks", "trousers"]),  # too many
        intent_json(keywords=["a", "b", "c", "d", "e"]),  # too many
    ],
)
def test_invalid_model_output_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidIntent) as caught:
        parse_model_output(raw, ASK)
    assert "jackets" not in str(caught.value)
    assert "parkas" not in str(caught.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"product_id": 1},
        {"product_ids": [1, 2]},
        {"slug": "ridgeline-rain-shell"},
        {"sku": "ADO-RRS-NAV-M"},
        {"price_minor": 1},
        {"max_price_minor": 100},
        {"price": "$1.00"},
        {"discount": 90},
        {"stock": 999},
        {"shipping_minor": 0},
        {"total": 1},
        {"cart": {"add": "ADO-RRS-NAV-M"}},
        {"payment": {"capture": True}},
        {"payer_email": "buyer@example.com"},
    ],
)
def test_a_reply_that_adds_any_other_field_is_rejected(extra: dict[str, object]) -> None:
    with pytest.raises(InvalidIntent):
        parse_model_output(intent_json(categories=["jackets"], **extra), ASK)


def test_keywords_the_shopper_did_not_write_are_dropped() -> None:
    for invented in (
        ["lightweight", "ridgeline", "ADO-RRS-NAV-M", "cheap"],
        ["light weight", "$129", "x" * 40, "navy"],
    ):
        raw = intent_json(categories=["jackets"], keywords=invented)
        kept = parse_model_output(raw, ASK).keywords
        assert kept == (("lightweight",) if "lightweight" in invented else ("navy",))


def test_keywords_are_normalised_before_comparison() -> None:
    assert parse_model_output(intent_json(keywords=["LIGHTWEIGHT"]), ASK).keywords == ("lightweight",)
    assert parse_model_output(intent_json(keywords=["léger"]), "Une veste légère, LÉGER").keywords == ("leger",)


def test_an_empty_intent_is_valid() -> None:
    assert parse_model_output(intent_json(), "hello").is_empty


# --- keyword fallback -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ASK, intent(categories=("jackets",), sizes=("M",), colours=("navy",))),
        ("Gray merino socks, size M", intent(categories=("socks",), sizes=("M",), colours=("grey",), tags=("merino",))),
        (
            "waterproof hiking trousers XL",
            intent(categories=("trousers",), sizes=("XL",), tags=("waterproof", "hiking")),
        ),
        ("a thermal base layer, extra large", intent(categories=("base_layers",), sizes=("XL",))),
        ("quick-dry tee for trail running", intent(tags=("quick_dry", "trail_running"))),
        ("one size beanie in black", intent(categories=("accessories",), sizes=("ONE_SIZE",), colours=("black",))),
    ],
)
def test_the_keyword_parser_maps_known_words(text: str, expected: ShoppingIntent) -> None:
    assert keyword_intent(text) == expected


def test_unrecognised_text_becomes_a_plain_text_search() -> None:
    assert keyword_intent("Summit shell!") == intent(categories=("jackets",))
    assert keyword_intent("ridgeline ember summit glacier fjord") == intent(
        keywords=("ridgeline", "ember", "summit", "glacier")
    )
    assert keyword_intent("!!") == intent()


def test_the_keyword_parser_ignores_single_letters_outside_size_phrases() -> None:
    assert keyword_intent("a jacket for my s trip").sizes == ()


# --- what the model may see -------------------------------------------------------------------------


def test_redaction_removes_contact_and_card_like_details() -> None:
    sent = redact("navy jacket, mail me at jane.doe@example.com or +33 6 12 34 56 78, card 4111 1111 1111 1111")
    assert "jane" not in sent
    assert "example.com" not in sent
    assert "12 34" not in sent
    assert "4111" not in sent
    assert sent.startswith("navy jacket")
    assert "[email]" in sent
    assert "[number]" in sent


def test_redaction_keeps_short_numbers_and_trims_the_length() -> None:
    assert redact("size 42 boots under 150") == "size 42 boots under 150"
    assert len(redact("x" * (MAX_QUERY_CHARS + 50))) == MAX_QUERY_CHARS
