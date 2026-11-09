import type { Cart, CartLine, Product, ProductPage } from "../lib/api.ts";

/** Shaped exactly like real GET /api/catalog/products responses (captured from the seeded backend). */
export function product(overrides: Partial<Product> = {}): Product {
  return {
    slug: "ridgeline-rain-shell",
    category: "jackets",
    tags: ["breathable", "hiking", "packable", "waterproof"],
    name: "Ridgeline Rain Shell",
    description:
      "Lightweight 2.5-layer waterproof jacket for hiking in changing weather. Packs into its own chest pocket.",
    lang: "en",
    fallback: false,
    machine_translated: false,
    price_range: { min: { amount_minor: 12900, currency: "USD" }, max: { amount_minor: 12900, currency: "USD" } },
    variants: [
      {
        sku: "ADO-RRS-NAV-S",
        size: "S",
        colour: "navy",
        price: { amount_minor: 12900, currency: "USD" },
        stock_status: "in_stock",
        matches_filters: true,
      },
      {
        sku: "ADO-RRS-NAV-M",
        size: "M",
        colour: "navy",
        price: { amount_minor: 12900, currency: "USD" },
        stock_status: "low_stock",
        matches_filters: true,
      },
    ],
    ...overrides,
  };
}

export function page(items: Product[], overrides: Partial<ProductPage> = {}): ProductPage {
  return { items, page: 1, page_size: 20, total: items.length, total_pages: items.length ? 1 : 0, ...overrides };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function errorResponse(status: number, code: string, message: string, details: unknown[] = []): Response {
  return jsonResponse({ error: { code, message, details } }, status);
}

/** Shaped exactly like real /api/cart responses (captured from the M2.3 backend). */
export function cartLine(overrides: Partial<CartLine> = {}): CartLine {
  return {
    sku: "ADO-RRS-NAV-M",
    product_slug: "ridgeline-rain-shell",
    name: "Ridgeline Rain Shell",
    lang: "en",
    fallback: false,
    machine_translated: false,
    size: "M",
    colour: "navy",
    quantity: 1,
    unit_price: { amount_minor: 12900, currency: "USD" },
    line_total: { amount_minor: 12900, currency: "USD" },
    available: true,
    ...overrides,
  };
}

export function cart(overrides: Partial<Cart> = {}): Cart {
  const lines = overrides.lines ?? [];
  return {
    lines,
    item_count: lines.reduce((sum, line) => sum + line.quantity, 0),
    subtotal: { amount_minor: 0, currency: "USD" },
    expires_at: lines.length ? "2026-12-09T14:31:18.138596Z" : null,
    reservation: null,
    ...overrides,
  };
}

type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

/**
 * Answers the app-wide cart read (GET /api/cart) with an empty cart and passes every other request
 * to `mock`, so page tests see exactly the requests they did before the bag existed.
 */
export function withCart(mock: FetchLike, body: Cart = cart()): FetchLike {
  return async (url, init) => (url.startsWith("/api/cart") ? jsonResponse(body) : mock(url, init));
}
