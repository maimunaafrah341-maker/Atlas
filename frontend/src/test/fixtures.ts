import type { Product, ProductPage } from "../lib/api.ts";

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
