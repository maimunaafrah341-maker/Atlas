"""Closed vocabularies for the catalogue (PRODUCT_SPEC F2, ARCHITECTURE §8.2).

Values are canonical, language-neutral codes. Human-readable labels per language belong to
the UI message catalogues (M7). Database CHECK constraints enforce the same lists; the
migration freezes its own copy, and tests prove every value here is accepted by the schema.
"""

from __future__ import annotations

from typing import Literal, get_args

Category = Literal["jackets", "base_layers", "trousers", "socks", "accessories"]
Tag = Literal[
    # features
    "waterproof",
    "windproof",
    "insulated",
    "packable",
    "breathable",
    "quick_dry",
    "merino",
    "uv_protection",
    # activities
    "hiking",
    "trekking",
    "trail_running",
    "camping",
    "travel",
    "winter",
]
# Listed in display order.
Size = Literal["XS", "S", "S_M", "M", "L", "L_XL", "XL", "XXL", "ONE_SIZE"]
Colour = Literal[
    "black", "charcoal", "grey", "white", "navy", "blue", "red", "orange", "yellow", "green", "olive", "khaki"
]
Language = Literal["en", "es", "fr", "de", "pt-BR"]
ProductStatus = Literal["draft", "active", "archived"]
VariantStatus = Literal["active", "archived"]
StockStatus = Literal["in_stock", "low_stock", "out_of_stock"]

CATEGORIES: tuple[str, ...] = get_args(Category)
TAGS: tuple[str, ...] = get_args(Tag)
SIZES: tuple[str, ...] = get_args(Size)
COLOURS: tuple[str, ...] = get_args(Colour)
LANGUAGES: tuple[str, ...] = get_args(Language)
PRODUCT_STATUSES: tuple[str, ...] = get_args(ProductStatus)
VARIANT_STATUSES: tuple[str, ...] = get_args(VariantStatus)

DEFAULT_LANGUAGE: Language = "en"
SIZE_ORDER: dict[str, int] = {size: index for index, size in enumerate(SIZES)}

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
SKU_PATTERN = r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$"


def sql_in(column: str, values: tuple[str, ...]) -> str:
    """SQL text for ``column IN ('a', 'b')`` from trusted constant values (CHECK constraints)."""
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column} IN ({quoted})"
