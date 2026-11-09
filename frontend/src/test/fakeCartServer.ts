import type { Cart, CartLine, CartReservation } from "../lib/api.ts";
import { cartLine, errorResponse, jsonResponse } from "./fixtures.ts";

/**
 * An in-memory stand-in for /api/cart that follows the M2.3 contract: the server computes totals,
 * any edit ends the reservation, repeating a reservation returns it unchanged (201 then 200), and a
 * read after expiry no longer shows it. Tests can make the next request fail with an API error or
 * a network failure. Other URLs go to `fallback`.
 */
export const CATALOGUE: Record<string, CartLine> = {
  "ADO-RRS-NAV-M": cartLine(),
  "ADO-RRS-NAV-L": cartLine({ sku: "ADO-RRS-NAV-L", size: "L" }),
  "SOCK-GRY-M": cartLine({
    sku: "SOCK-GRY-M",
    product_slug: "merino-hiking-socks",
    name: "Merino Hiking Socks",
    colour: "grey",
    unit_price: { amount_minor: 2400, currency: "USD" },
    line_total: { amount_minor: 2400, currency: "USD" },
  }),
};

type Failure =
  | { kind: "network" }
  | { kind: "api"; status: number; code: string; message?: string; details?: Record<string, unknown>[] };

export interface FakeCartServer {
  fetch: (url: string, init?: RequestInit) => Promise<Response>;
  calls: string[];
  lines: Map<string, CartLine>;
  reservation: CartReservation | null;
  /** Make the next cart request (optionally only for this method) fail. */
  failNext: (failure: Failure, method?: string) => void;
  /** Mark a SKU archived on the "server". */
  archive: (sku: string) => void;
  /** Hold responses until released, to test ordering. */
  hold: () => () => void;
  ttlMs: number;
  subtotalOverride: number | null;
}

export function fakeCartServer(
  initial: { sku: string; quantity: number }[] = [],
  fallback: (url: string, init?: RequestInit) => Promise<Response> = async () => jsonResponse({}),
): FakeCartServer {
  const failures: { failure: Failure; method?: string }[] = [];
  let gate: Promise<void> | null = null;

  const server: FakeCartServer = {
    calls: [],
    lines: new Map(),
    reservation: null,
    ttlMs: 15 * 60 * 1000,
    subtotalOverride: null,
    failNext: (failure, method) => failures.push({ failure, method }),
    archive: (sku) => {
      const line = server.lines.get(sku);
      if (line) server.lines.set(sku, { ...line, available: false });
    },
    hold: () => {
      let release = () => {};
      gate = new Promise((resolve) => (release = resolve));
      return () => {
        gate = null;
        release();
      };
    },
    fetch: async (url, init) => {
      if (!url.startsWith("/api/cart")) return fallback(url, init);
      const method = init?.method ?? "GET";
      const path = url.split("?")[0]!;
      const lang = new URL(url, "http://x").searchParams.get("lang") ?? "en";
      server.calls.push(`${method} ${url}`);
      if (gate) await gate;

      const index = failures.findIndex((f) => !f.method || f.method === method);
      if (index !== -1) {
        const failure = failures.splice(index, 1)[0]!.failure;
        if (failure.kind === "network") throw new TypeError("Failed to fetch");
        return errorResponse(failure.status, failure.code, failure.message ?? failure.code, failure.details ?? []);
      }

      if (server.reservation && Date.parse(server.reservation.expires_at) <= Date.now()) server.reservation = null;
      const edit = () => (server.reservation = null);
      const item = /^\/api\/cart\/items\/(.+)$/.exec(path);

      if (item && method === "PUT") {
        const sku = decodeURIComponent(item[1]!);
        const quantity = (JSON.parse(String(init?.body)) as { quantity: number }).quantity;
        const base = server.lines.get(sku) ?? CATALOGUE[sku];
        if (!base) return errorResponse(404, "variant_not_found", "No such variant.");
        if (quantity < 1 || quantity > 10) return errorResponse(422, "invalid_quantity", "Quantity must be between 1 and 10.");
        if (base.quantity !== quantity || !server.lines.has(sku)) edit();
        server.lines.set(sku, { ...base, quantity });
      } else if (item && method === "DELETE") {
        if (server.lines.delete(decodeURIComponent(item[1]!))) edit();
      } else if (path === "/api/cart/reservation" && method === "POST") {
        if (server.lines.size === 0) return errorResponse(409, "cart_empty", "The cart is empty.");
        const unavailable = [...server.lines.values()].filter((line) => !line.available);
        if (unavailable.length)
          return errorResponse(409, "cart_has_unavailable_items", "Some items are no longer available.", unavailable.map((l) => ({ sku: l.sku })));
        if (server.reservation) return jsonResponse(view(server, lang), 200);
        server.reservation = {
          id: "res-public-1",
          status: "active",
          expires_at: new Date(Date.now() + server.ttlMs).toISOString(),
          items: [...server.lines.values()].map((line) => ({ sku: line.sku, quantity: line.quantity })),
        };
        return jsonResponse(view(server, lang), 201);
      } else if (path === "/api/cart/reservation" && method === "DELETE") {
        server.reservation = null;
      }
      return jsonResponse(view(server, lang));
    },
  };
  for (const { sku, quantity } of initial) server.lines.set(sku, { ...CATALOGUE[sku]!, quantity });
  return server;
}

function view(server: FakeCartServer, lang: string): Cart {
  const lines = [...server.lines.values()].map((line) => {
    const total = line.unit_price.amount_minor * line.quantity;
    const translated = lang !== "en" && line.sku === "ADO-RRS-NAV-M";
    return {
      ...line,
      name: translated ? "Ridgeline Regenjacke" : line.name,
      lang: (translated ? lang : "en") as CartLine["lang"],
      fallback: lang !== "en" && !translated,
      machine_translated: translated,
      line_total: { amount_minor: total, currency: "USD" },
    };
  });
  const subtotal = lines.filter((line) => line.available).reduce((sum, line) => sum + line.line_total.amount_minor, 0);
  return {
    lines,
    item_count: lines.reduce((sum, line) => sum + line.quantity, 0),
    subtotal: { amount_minor: server.subtotalOverride ?? subtotal, currency: "USD" },
    expires_at: lines.length ? "2026-12-09T14:31:18Z" : null,
    reservation: server.reservation,
  };
}
