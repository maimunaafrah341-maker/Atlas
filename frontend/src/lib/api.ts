/** Typed client for the Atlas API. Same origin in every environment; no secrets here. */

import { isCategory, isLanguage, type Category, type Colour, type Language, type Size, type SortOrder } from "./catalogue.ts";

export type { SortOrder };

// --- Health -----------------------------------------------------------------------------------

export interface DatabaseHealth {
  status: "ok" | "error";
  dialect: string;
  latency_ms: number | null;
  migration_revision: string | null;
  migration_head: string | null;
  schema_up_to_date: boolean;
}

export interface HealthReport {
  status: "ok" | "degraded";
  version: string;
  environment: string;
  database: DatabaseHealth;
}

// --- Catalogue (backend app/domain/catalog/schemas.py) ---------------------------------------

/** Integer minor units (USD cents) plus an ISO 4217 code. Never a float, never pre-formatted. */
export interface Money {
  amount_minor: number;
  currency: string;
}

export type StockStatus = "in_stock" | "low_stock" | "out_of_stock";

export interface Variant {
  sku: string;
  size: string;
  colour: string;
  price: Money;
  /** Never a count (D-5). */
  stock_status: StockStatus;
  matches_filters: boolean;
}

export interface Product {
  slug: string;
  category: Category;
  tags: string[];
  name: string;
  description: string;
  /** The language the text is actually in. */
  lang: Language;
  /** True when the requested language had no translation and English was used instead. */
  fallback: boolean;
  /** True when the text is a machine translation (not reviewed). */
  machine_translated: boolean;
  price_range: { min: Money; max: Money };
  variants: Variant[];
}

export interface ProductPage {
  items: Product[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

/** Query parameters of GET /api/catalog/products. Lists are sent as repeated parameters. */
export interface ProductQuery {
  q?: string;
  lang?: Language;
  category?: Category[];
  tag?: string[];
  size?: Size[];
  colour?: Colour[];
  min_price_minor?: number;
  max_price_minor?: number;
  in_stock?: boolean;
  sort?: SortOrder;
  page?: number;
  page_size?: number;
}

// --- Errors and transport ---------------------------------------------------------------------

export class ApiError extends Error {
  readonly status: number | null;
  /** The API's error code (e.g. "product_not_found"), when the response carried one. */
  readonly code: string | null;
  readonly details: Record<string, unknown>[];

  constructor(message: string, status: number | null, code: string | null = null, details: Record<string, unknown>[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

// A sleeping Render free instance can take about a minute to wake, so allow well beyond that.
const DEFAULT_TIMEOUT_MS = 90_000;

interface RawResponse {
  status: number;
  ok: boolean;
  body: unknown;
}

interface RequestOptions {
  method?: "GET" | "PUT" | "POST" | "DELETE";
  /** Sent as JSON. */
  body?: unknown;
}

/**
 * Request a JSON resource. Network failures and timeouts become ApiError; the body may be null.
 * Same-origin only: the browser sends cookies (the HttpOnly cart cookie) and, on PUT/POST/DELETE,
 * the Origin header the backend's same-origin check needs. Nothing here reads or stores cookies.
 */
async function requestJson(
  path: string,
  signal: AbortSignal | undefined,
  timeoutMs: number,
  { method = "GET", body: payload }: RequestOptions = {},
): Promise<RawResponse> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (payload !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      signal: combined,
      headers,
      credentials: "same-origin",
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(timeout.aborted ? "The server took too long to respond." : "Could not reach the server.", null);
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Fall through to the shape checks.
  }
  return { status: response.status, ok: response.ok, body };
}

/** The API's error envelope: {"error": {"code", "message", "details"}}. */
function errorFrom(raw: RawResponse): ApiError {
  const error = isRecord(raw.body) && isRecord(raw.body.error) ? raw.body.error : null;
  if (error && typeof error.code === "string" && typeof error.message === "string") {
    const details = Array.isArray(error.details) ? error.details.filter(isRecord) : [];
    return new ApiError(error.message, raw.status, error.code, details);
  }
  return new ApiError(`Unexpected response from the server (HTTP ${raw.status}).`, raw.status);
}

// --- Shape checks -----------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isMoney(value: unknown): value is Money {
  return isRecord(value) && Number.isInteger(value.amount_minor) && typeof value.currency === "string";
}

const STOCK_STATUSES: readonly string[] = ["in_stock", "low_stock", "out_of_stock"];

function isVariant(value: unknown): value is Variant {
  return (
    isRecord(value) &&
    typeof value.sku === "string" &&
    typeof value.size === "string" &&
    typeof value.colour === "string" &&
    isMoney(value.price) &&
    typeof value.stock_status === "string" &&
    STOCK_STATUSES.includes(value.stock_status) &&
    typeof value.matches_filters === "boolean"
  );
}

export function isProduct(value: unknown): value is Product {
  return (
    isRecord(value) &&
    typeof value.slug === "string" &&
    isCategory(value.category) &&
    Array.isArray(value.tags) &&
    value.tags.every((tag) => typeof tag === "string") &&
    typeof value.name === "string" &&
    typeof value.description === "string" &&
    isLanguage(value.lang) &&
    typeof value.fallback === "boolean" &&
    typeof value.machine_translated === "boolean" &&
    isRecord(value.price_range) &&
    isMoney(value.price_range.min) &&
    isMoney(value.price_range.max) &&
    Array.isArray(value.variants) &&
    value.variants.every(isVariant)
  );
}

function isProductPage(value: unknown): value is ProductPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isProduct) &&
    Number.isInteger(value.page) &&
    Number.isInteger(value.page_size) &&
    Number.isInteger(value.total) &&
    Number.isInteger(value.total_pages)
  );
}

function isHealthReport(value: unknown): value is HealthReport {
  if (typeof value !== "object" || value === null) return false;
  const report = value as Record<string, unknown>;
  return (
    (report.status === "ok" || report.status === "degraded") &&
    typeof report.version === "string" &&
    typeof report.database === "object" &&
    report.database !== null
  );
}

// --- Endpoints --------------------------------------------------------------------------------

/**
 * Readiness report. A 503 still carries a valid report (status "degraded"), so it is
 * returned rather than thrown; network failures and malformed responses throw ApiError.
 */
export async function getHealth(signal?: AbortSignal, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<HealthReport> {
  const raw = await requestJson("/api/health", signal, timeoutMs);
  if ((raw.ok || raw.status === 503) && isHealthReport(raw.body)) return raw.body;
  throw new ApiError(`Unexpected response from the server (HTTP ${raw.status}).`, raw.status);
}

/** The query string for GET /api/catalog/products; list values become repeated parameters. */
export function productQueryString(query: ProductQuery): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query) as [string, unknown][]) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((item) => params.append(key, String(item)));
    else params.set(key, String(value));
  }
  return params.toString();
}

/** GET /api/catalog/products: search, filter, sort and paginate the catalogue. */
export async function listProducts(
  query: ProductQuery = {},
  signal?: AbortSignal,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<ProductPage> {
  const qs = productQueryString(query);
  const raw = await requestJson(`/api/catalog/products${qs ? `?${qs}` : ""}`, signal, timeoutMs);
  if (!raw.ok) throw errorFrom(raw);
  if (!isProductPage(raw.body)) throw new ApiError("Unexpected catalogue response from the server.", raw.status);
  return raw.body;
}

/** GET /api/catalog/products/{slug}. A missing product throws ApiError with code "product_not_found". */
export async function getProduct(
  slug: string,
  lang?: Language,
  signal?: AbortSignal,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<Product> {
  const qs = lang ? `?${new URLSearchParams({ lang })}` : "";
  const raw = await requestJson(`/api/catalog/products/${encodeURIComponent(slug)}${qs}`, signal, timeoutMs);
  if (!raw.ok) throw errorFrom(raw);
  if (!isProduct(raw.body)) throw new ApiError("Unexpected product response from the server.", raw.status);
  return raw.body;
}

// --- Shipping (backend app/api/shipping.py) ----------------------------------------------------

/** A flat rate from the merchant's zone table for one destination country. */
export interface ShippingQuote {
  country: string;
  zone: string;
  zone_name: string;
  price: Money;
}

function isShippingQuote(value: unknown): value is ShippingQuote {
  return (
    isRecord(value) &&
    typeof value.country === "string" &&
    typeof value.zone === "string" &&
    typeof value.zone_name === "string" &&
    isMoney(value.price)
  );
}

/**
 * GET /api/shipping/quote?country=XX. A country outside every zone throws ApiError with code
 * "unsupported_destination"; a malformed code, "invalid_country_code" (both 422).
 */
export async function getShippingQuote(
  country: string,
  signal?: AbortSignal,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<ShippingQuote> {
  const raw = await requestJson(`/api/shipping/quote?${new URLSearchParams({ country })}`, signal, timeoutMs);
  if (!raw.ok) throw errorFrom(raw);
  if (!isShippingQuote(raw.body)) throw new ApiError("Unexpected shipping response from the server.", raw.status);
  return raw.body;
}

// --- Cart (backend app/api/cart.py, M2.3) -----------------------------------------------------

/** One cart line. Prices are live catalogue prices; totals are computed by the server. */
export interface CartLine {
  sku: string;
  product_slug: string;
  name: string;
  lang: Language;
  fallback: boolean;
  machine_translated: boolean;
  size: string;
  colour: string;
  quantity: number;
  unit_price: Money;
  line_total: Money;
  /** False once the variant or its product is archived; the server leaves it out of the subtotal. */
  available: boolean;
}

export interface CartReservation {
  /** The reservation's public id. */
  id: string;
  status: string;
  /** ISO 8601, UTC. */
  expires_at: string;
  items: { sku: string; quantity: number }[];
}

export interface Cart {
  lines: CartLine[];
  item_count: number;
  subtotal: Money;
  /** When the cart itself lapses (30 days after its last change); null for an empty, cookie-less cart. */
  expires_at: string | null;
  reservation: CartReservation | null;
}

function isCartLine(value: unknown): value is CartLine {
  return (
    isRecord(value) &&
    typeof value.sku === "string" &&
    typeof value.product_slug === "string" &&
    typeof value.name === "string" &&
    isLanguage(value.lang) &&
    typeof value.fallback === "boolean" &&
    typeof value.machine_translated === "boolean" &&
    typeof value.size === "string" &&
    typeof value.colour === "string" &&
    Number.isInteger(value.quantity) &&
    isMoney(value.unit_price) &&
    isMoney(value.line_total) &&
    typeof value.available === "boolean"
  );
}

function isReservation(value: unknown): value is CartReservation {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.status === "string" &&
    typeof value.expires_at === "string" &&
    !Number.isNaN(Date.parse(value.expires_at)) &&
    Array.isArray(value.items) &&
    value.items.every((item) => isRecord(item) && typeof item.sku === "string" && Number.isInteger(item.quantity))
  );
}

export function isCart(value: unknown): value is Cart {
  return (
    isRecord(value) &&
    Array.isArray(value.lines) &&
    value.lines.every(isCartLine) &&
    Number.isInteger(value.item_count) &&
    isMoney(value.subtotal) &&
    (value.expires_at === null || typeof value.expires_at === "string") &&
    (value.reservation === null || isReservation(value.reservation))
  );
}

async function cartRequest(
  path: string,
  lang: Language,
  options: RequestOptions,
  signal?: AbortSignal,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<{ cart: Cart; status: number }> {
  const raw = await requestJson(`/api/cart${path}?${new URLSearchParams({ lang })}`, signal, timeoutMs, options);
  if (!raw.ok) throw errorFrom(raw);
  if (!isCart(raw.body)) throw new ApiError("Unexpected cart response from the server.", raw.status);
  return { cart: raw.body, status: raw.status };
}

/** GET /api/cart: the current cart (empty when there is none). Never creates a cart. */
export async function getCart(lang: Language, signal?: AbortSignal, timeoutMs?: number): Promise<Cart> {
  return (await cartRequest("", lang, {}, signal, timeoutMs)).cart;
}

/** PUT /api/cart/items/{sku}: set a line's quantity (1–10). The first add creates the cart. */
export async function setCartQuantity(sku: string, quantity: number, lang: Language, timeoutMs?: number): Promise<Cart> {
  const path = `/items/${encodeURIComponent(sku)}`;
  return (await cartRequest(path, lang, { method: "PUT", body: { quantity } }, undefined, timeoutMs)).cart;
}

/** DELETE /api/cart/items/{sku}: remove a line (removing a missing line succeeds). */
export async function removeCartLine(sku: string, lang: Language, timeoutMs?: number): Promise<Cart> {
  return (await cartRequest(`/items/${encodeURIComponent(sku)}`, lang, { method: "DELETE" }, undefined, timeoutMs)).cart;
}

/**
 * POST /api/cart/reservation: hold the cart's stock. Idempotent: repeating it returns the same
 * reservation unchanged (never renewed); `created` is false then.
 */
export async function reserveCart(lang: Language, timeoutMs?: number): Promise<{ cart: Cart; created: boolean }> {
  const { cart, status } = await cartRequest("/reservation", lang, { method: "POST" }, undefined, timeoutMs);
  return { cart, created: status === 201 };
}

/** DELETE /api/cart/reservation: release the hold (releasing with nothing held succeeds). */
export async function releaseCart(lang: Language, timeoutMs?: number): Promise<Cart> {
  return (await cartRequest("/reservation", lang, { method: "DELETE" }, undefined, timeoutMs)).cart;
}

// --- Discovery (backend app/api/discovery.py, M5) ---------------------------------------------

/** What the shopper asked for, in catalogue codes. Produced by Gemini or the keyword parser. */
export interface DiscoveryIntent {
  categories: Category[];
  sizes: Size[];
  colours: Colour[];
  tags: string[];
  keywords: string[];
}

export interface Discovery {
  /** "gemini": Gemini's intent, validated by the backend. "keywords": the deterministic fallback. */
  source: "gemini" | "keywords";
  /** The Gemini model id, when source is "gemini". */
  model: string | null;
  /** Why the keyword parser was used ("not_configured", "invalid_output", "unavailable: …"). */
  fallback_reason: string | null;
  intent: DiscoveryIntent;
  /** Constraints the backend dropped because nothing matched them. */
  relaxed: ("keywords" | "tags")[];
  /** Products from the ordinary catalogue search, exactly as on the Shop page. */
  results: ProductPage;
}

/** The longest request the API accepts. */
export const MAX_DISCOVERY_LENGTH = 300;

const isStrings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");

function isDiscovery(value: unknown): value is Discovery {
  const intent = isRecord(value) ? value.intent : null;
  return (
    isRecord(value) &&
    (value.source === "gemini" || value.source === "keywords") &&
    (value.model === null || typeof value.model === "string") &&
    (value.fallback_reason === null || typeof value.fallback_reason === "string") &&
    isRecord(intent) &&
    Array.isArray(intent.categories) &&
    intent.categories.every(isCategory) &&
    isStrings(intent.sizes) &&
    isStrings(intent.colours) &&
    isStrings(intent.tags) &&
    isStrings(intent.keywords) &&
    isStrings(value.relaxed) &&
    isProductPage(value.results)
  );
}

/**
 * POST /api/discovery: describe what you need in your own words. The backend asks Gemini for a
 * structured intent (or falls back to keyword matching) and returns real catalogue products.
 * Read-only: it never changes the bag.
 */
export async function discoverProducts(
  query: string,
  lang: Language,
  signal?: AbortSignal,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<Discovery> {
  const raw = await requestJson("/api/discovery", signal, timeoutMs, { method: "POST", body: { query, lang } });
  if (!raw.ok) throw errorFrom(raw);
  if (!isDiscovery(raw.body)) throw new ApiError("Unexpected discovery response from the server.", raw.status);
  return raw.body;
}
