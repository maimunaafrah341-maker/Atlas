/**
 * The shop's filter state and its URL form. The URL is the single source of truth: the page
 * parses it on every render, and every change writes a new URL (a history entry), so refresh,
 * deep links and back/forward all work without separate state.
 *
 * URL parameters (only non-default values are written):
 *   q            search text
 *   category     repeated; backend categories
 *   size         repeated; backend sizes
 *   colour       repeated; backend colours
 *   min_price    whole US dollars (the API takes cents: min_price_minor)
 *   max_price    whole US dollars (max_price_minor)
 *   in_stock     "1"
 *   sort         a backend sort order other than the default for the search
 *   page         2 and up
 * Unknown or invalid values are ignored rather than sent to the API.
 */

import type { ProductQuery } from "../../lib/api.ts";
import {
  CATEGORIES,
  COLOURS,
  MAX_PRICE_MINOR,
  SIZES,
  isCategory,
  isColour,
  isSize,
  isSortOrder,
  type Category,
  type Colour,
  type Language,
  type Size,
  type SortOrder,
} from "../../lib/catalogue.ts";

/** 12 fills complete rows at two, three and four columns. */
export const SHOP_PAGE_SIZE = 12;
/** The API's limits: q at most 200 characters, page at most 10,000, prices at most MAX_PRICE_MINOR cents. */
export const MAX_QUERY_LENGTH = 200;
export const MAX_PAGE = 10_000;
export const MAX_PRICE_USD = MAX_PRICE_MINOR / 100;

export interface ShopFilters {
  q: string;
  categories: Category[];
  sizes: Size[];
  colours: Colour[];
  /** Whole US dollars. */
  minPrice: number | null;
  maxPrice: number | null;
  inStock: boolean;
  /** An explicit sort; null means the default for the search (see effectiveSort). */
  sort: SortOrder | null;
  page: number;
}

export const NO_FILTERS: ShopFilters = {
  q: "",
  categories: [],
  sizes: [],
  colours: [],
  minPrice: null,
  maxPrice: null,
  inStock: false,
  sort: null,
  page: 1,
};

/** Valid, distinct values, in the vocabulary's own order (so equivalent URLs give one request). */
function pick<T extends string>(values: string[], vocabulary: readonly T[], valid: (value: unknown) => value is T): T[] {
  const chosen = new Set(values.filter(valid));
  return vocabulary.filter((value) => chosen.has(value));
}

function wholeNumber(raw: string | null, max: number): number | null {
  if (raw === null || !/^\d{1,9}$/.test(raw)) return null;
  const value = Number(raw);
  return value <= max ? value : null;
}

/**
 * Mirrors the backend's rule (app/domain/catalog/text.py): a search needs at least one word of
 * two or more letters or digits after accents are removed. Anything else is answered with 422.
 */
export function isSearchable(q: string): boolean {
  const folded = q.normalize("NFKD").replace(/\p{M}/gu, "");
  return /[\p{L}\p{N}]{2,}/u.test(folded);
}

export function parseShopParams(params: URLSearchParams): ShopFilters {
  const q = (params.get("q") ?? "").trim().slice(0, MAX_QUERY_LENGTH);
  const sortParam = params.get("sort");
  let sort = isSortOrder(sortParam) ? sortParam : null;
  if (sort === "relevance" && !q) sort = null;
  return {
    q,
    categories: pick(params.getAll("category"), CATEGORIES, isCategory),
    sizes: pick(params.getAll("size"), SIZES, isSize),
    colours: pick(params.getAll("colour"), COLOURS, isColour),
    minPrice: wholeNumber(params.get("min_price"), MAX_PRICE_USD),
    maxPrice: wholeNumber(params.get("max_price"), MAX_PRICE_USD),
    inStock: params.get("in_stock") === "1" || params.get("in_stock") === "true",
    sort,
    page: Math.max(1, wholeNumber(params.get("page"), MAX_PAGE) ?? 1),
  };
}

/** The canonical URL parameters for a filter state; defaults are left out. */
export function shopParams(filters: ShopFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  filters.categories.forEach((value) => params.append("category", value));
  filters.sizes.forEach((value) => params.append("size", value));
  filters.colours.forEach((value) => params.append("colour", value));
  if (filters.minPrice !== null) params.set("min_price", String(filters.minPrice));
  if (filters.maxPrice !== null) params.set("max_price", String(filters.maxPrice));
  if (filters.inStock) params.set("in_stock", "1");
  if (filters.sort && filters.sort !== defaultSort(filters.q)) params.set("sort", filters.sort);
  if (filters.page > 1) params.set("page", String(filters.page));
  return params;
}

/** The backend's default: relevance when searching, featured otherwise. */
export function defaultSort(q: string): SortOrder {
  return q ? "relevance" : "featured";
}

export function effectiveSort(filters: Pick<ShopFilters, "q" | "sort">): SortOrder {
  return filters.sort ?? defaultSort(filters.q);
}

/**
 * Apply a change. Any change other than the page itself returns to page 1, and clearing the
 * search drops a relevance sort (which the API only accepts with a search).
 */
export function changeFilters(filters: ShopFilters, patch: Partial<ShopFilters>): ShopFilters {
  const next: ShopFilters = { ...filters, page: 1, ...patch };
  if (!next.q && next.sort === "relevance") next.sort = null;
  if (next.sort === defaultSort(next.q)) next.sort = null;
  return next;
}

/** The API request for a filter state. Whole dollars become cents; the sort is left to the API's default. */
export function toProductQuery(filters: ShopFilters, lang: Language): ProductQuery {
  return {
    q: filters.q || undefined,
    lang,
    category: filters.categories,
    size: filters.sizes,
    colour: filters.colours,
    min_price_minor: filters.minPrice === null ? undefined : filters.minPrice * 100,
    max_price_minor: filters.maxPrice === null ? undefined : filters.maxPrice * 100,
    in_stock: filters.inStock || undefined,
    sort: filters.sort ?? undefined,
    page: filters.page,
    page_size: SHOP_PAGE_SIZE,
  };
}

/** Filters that narrow the results (search, sort and page are shown separately). */
export function activeFilterCount(filters: ShopFilters): number {
  return (
    filters.categories.length +
    filters.sizes.length +
    filters.colours.length +
    (filters.minPrice !== null || filters.maxPrice !== null ? 1 : 0) +
    (filters.inStock ? 1 : 0)
  );
}

/** Filters the API applies per variant (sizes, colours, price, stock), which set `matches_filters`. */
export function hasVariantFilters(filters: ShopFilters): boolean {
  return (
    filters.sizes.length > 0 ||
    filters.colours.length > 0 ||
    filters.minPrice !== null ||
    filters.maxPrice !== null ||
    filters.inStock
  );
}

export function hasAnyFilter(filters: ShopFilters): boolean {
  return activeFilterCount(filters) > 0 || filters.q !== "";
}
