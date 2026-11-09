/**
 * The catalogue API's closed vocabularies (backend app/domain/catalog/vocab.py).
 *
 * These are part of the API contract — the values the query parameters accept — not catalogue
 * data: products, prices, variants and stock always come from the API. The English labels are
 * UI copy (full interface translation arrives in M7). `catalogue.contract.test.ts` fails if
 * these lists drift from the backend.
 */

export const LANGUAGES = ["en", "es", "fr", "de", "pt-BR"] as const;
export type Language = (typeof LANGUAGES)[number];
export const DEFAULT_LANGUAGE: Language = "en";

/** Each language's name in itself, as language pickers conventionally show it. */
export const LANGUAGE_NAMES: Record<Language, string> = {
  en: "English",
  es: "Español",
  fr: "Français",
  de: "Deutsch",
  "pt-BR": "Português (Brasil)",
};

export const CATEGORIES = ["jackets", "base_layers", "trousers", "socks", "accessories"] as const;
export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_LABELS: Record<Category, string> = {
  jackets: "Jackets",
  base_layers: "Base layers",
  trousers: "Trousers",
  socks: "Socks",
  accessories: "Accessories",
};

export function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (LANGUAGES as readonly string[]).includes(value);
}

export function isCategory(value: unknown): value is Category {
  return typeof value === "string" && (CATEGORIES as readonly string[]).includes(value);
}

/** Variant sizes, in the backend's display order. */
export const SIZES = ["XS", "S", "S_M", "M", "L", "L_XL", "XL", "XXL", "ONE_SIZE"] as const;
export type Size = (typeof SIZES)[number];

export const COLOURS = [
  "black",
  "charcoal",
  "grey",
  "white",
  "navy",
  "blue",
  "red",
  "orange",
  "yellow",
  "green",
  "olive",
  "khaki",
] as const;
export type Colour = (typeof COLOURS)[number];

/**
 * A swatch for each colour code, used only to illustrate filter options. Presentation, not
 * product data: it says nothing about any product's actual shade.
 */
export const COLOUR_SWATCHES: Record<Colour, string> = {
  black: "#1c1c1c",
  charcoal: "#3d4140",
  grey: "#8e918d",
  white: "#fbfaf6",
  navy: "#1f2c4a",
  blue: "#3c64a0",
  red: "#a3362b",
  orange: "#d0702e",
  yellow: "#d9b23a",
  green: "#3e6e48",
  olive: "#6b6b3a",
  khaki: "#b5a47c",
};

/** Sort orders of GET /api/catalog/products (backend service.py). "relevance" requires a search. */
export const SORT_ORDERS = ["featured", "relevance", "price_asc", "price_desc"] as const;
export type SortOrder = (typeof SORT_ORDERS)[number];

function oneOf<T extends string>(values: readonly T[]) {
  return (value: unknown): value is T => typeof value === "string" && (values as readonly string[]).includes(value);
}

export const isSize = oneOf(SIZES);
export const isColour = oneOf(COLOURS);
export const isSortOrder = oneOf(SORT_ORDERS);

/** The largest price filter the API accepts, in USD cents (backend service.py). */
export const MAX_PRICE_MINOR = 10_000_000;

/** Product tags: features first, then activities (backend order). */
export const TAGS = [
  "waterproof",
  "windproof",
  "insulated",
  "packable",
  "breathable",
  "quick_dry",
  "merino",
  "uv_protection",
  "hiking",
  "trekking",
  "trail_running",
  "camping",
  "travel",
  "winter",
] as const;
export type Tag = (typeof TAGS)[number];

export const TAG_LABELS: Record<Tag, string> = {
  waterproof: "Waterproof",
  windproof: "Windproof",
  insulated: "Insulated",
  packable: "Packable",
  breathable: "Breathable",
  quick_dry: "Quick-dry",
  merino: "Merino",
  uv_protection: "UV protection",
  hiking: "Hiking",
  trekking: "Trekking",
  trail_running: "Trail running",
  camping: "Camping",
  travel: "Travel",
  winter: "Winter",
};

export const isTag = oneOf(TAGS);
