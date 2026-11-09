import { afterEach, describe, expect, it, vi } from "vitest";
import { cart, cartLine, errorResponse, jsonResponse, page, product } from "../test/fixtures.ts";
import {
  ApiError,
  getCart,
  getProduct,
  getShippingQuote,
  listProducts,
  productQueryString,
  releaseCart,
  removeCartLine,
  reserveCart,
  setCartQuantity,
} from "./api.ts";

function mockFetch(response: Response | (() => Promise<Response>)) {
  const fetchMock = vi.fn(typeof response === "function" ? response : async () => response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("productQueryString", () => {
  it("sends lists as repeated parameters and skips empty values", () => {
    expect(
      productQueryString({
        q: "rain jacket",
        lang: "pt-BR",
        category: ["jackets", "socks"],
        size: ["S_M"],
        in_stock: true,
        min_price_minor: 0,
        sort: "price_asc",
        page: 2,
        page_size: 12,
        tag: [],
        colour: undefined,
      }),
    ).toBe(
      "q=rain+jacket&lang=pt-BR&category=jackets&category=socks&size=S_M&in_stock=true&min_price_minor=0&sort=price_asc&page=2&page_size=12",
    );
  });

  it("is empty for no query", () => {
    expect(productQueryString({})).toBe("");
  });
});

describe("listProducts", () => {
  it("requests the catalogue and returns a validated page", async () => {
    const body = page([product()], { page_size: 4 });
    const fetchMock = mockFetch(jsonResponse(body));
    await expect(listProducts({ lang: "de", page_size: 4 })).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith("/api/catalog/products?lang=de&page_size=4", expect.anything());
  });

  it("omits the query string when there is no query", async () => {
    const fetchMock = mockFetch(jsonResponse(page([])));
    await listProducts();
    expect(fetchMock).toHaveBeenCalledWith("/api/catalog/products", expect.anything());
  });

  it("surfaces the API's error code, message and details", async () => {
    mockFetch(
      errorResponse(422, "invalid_request", "Invalid value for sort: relevance requires q", [
        { field: "sort", issue: "relevance requires q" },
      ]),
    );
    const error = await listProducts({ sort: "relevance" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      code: "invalid_request",
      message: "Invalid value for sort: relevance requires q",
      details: [{ field: "sort", issue: "relevance requires q" }],
    });
  });

  it.each([
    ["a price that isn't integer minor units", { price_range: { min: { amount_minor: 129.0001, currency: "USD" }, max: { amount_minor: 12900, currency: "USD" } } }],
    ["an unknown stock status", { variants: [{ ...product().variants[0], stock_status: "plenty" }] }],
    ["an unknown category", { category: "hats" }],
    ["a missing translation flag", { machine_translated: undefined }],
  ])("rejects a response with %s", async (_label, overrides) => {
    mockFetch(jsonResponse(page([{ ...product(), ...(overrides as object) } as never])));
    await expect(listProducts()).rejects.toThrow("Unexpected catalogue response from the server.");
  });

  it("reports a non-JSON server error without leaking its body", async () => {
    mockFetch(new Response("<html>Bad gateway</html>", { status: 502 }));
    await expect(listProducts()).rejects.toMatchObject({ status: 502, code: null, message: "Unexpected response from the server (HTTP 502)." });
  });

  it("turns network failures into a readable ApiError", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(listProducts()).rejects.toMatchObject({ status: null, message: "Could not reach the server." });
  });

  it("times out instead of waiting forever", async () => {
    // A server that never answers: the request rejects only when its signal aborts.
    vi.stubGlobal(
      "fetch",
      (_: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    await expect(listProducts({}, undefined, 10)).rejects.toMatchObject({ message: "The server took too long to respond." });
  });
});

describe("getProduct", () => {
  it("encodes the slug and passes the language", async () => {
    const fetchMock = mockFetch(jsonResponse(product({ lang: "de", machine_translated: true })));
    const result = await getProduct("ridgeline-rain-shell", "de");
    expect(result.machine_translated).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/api/catalog/products/ridgeline-rain-shell?lang=de", expect.anything());
  });

  it("reports a missing product with its API code", async () => {
    mockFetch(errorResponse(404, "product_not_found", "No product nope.", [{ slug: "nope" }]));
    await expect(getProduct("nope")).rejects.toMatchObject({ status: 404, code: "product_not_found" });
  });
});

describe("getShippingQuote", () => {
  it("requests a quote for the country and validates it", async () => {
    const body = { country: "DE", zone: "europe", zone_name: "Europe", price: { amount_minor: 2000, currency: "USD" } };
    const fetchMock = mockFetch(jsonResponse(body));
    await expect(getShippingQuote("DE")).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith("/api/shipping/quote?country=DE", expect.anything());
  });

  it("reports an unsupported destination with its API code", async () => {
    mockFetch(errorResponse(422, "unsupported_destination", "We do not ship to IN.", [{ country: "IN" }]));
    await expect(getShippingQuote("IN")).rejects.toMatchObject({ status: 422, code: "unsupported_destination" });
  });

  it("rejects a malformed quote", async () => {
    mockFetch(jsonResponse({ country: "DE", price: { amount_minor: 20.5, currency: "USD" } }));
    await expect(getShippingQuote("DE")).rejects.toThrow("Unexpected shipping response from the server.");
  });
});

describe("cart", () => {
  const filled = cart({
    lines: [cartLine({ quantity: 2, line_total: { amount_minor: 25800, currency: "USD" } })],
    subtotal: { amount_minor: 25800, currency: "USD" },
  });
  const reserved = {
    ...filled,
    reservation: {
      id: "5c6c6621d537495dad9dc23cb7b00292",
      status: "active",
      expires_at: "2026-11-09T14:46:18.239159Z",
      items: [{ sku: "ADO-RRS-NAV-M", quantity: 2 }],
    },
  };
  const call = (fetchMock: ReturnType<typeof mockFetch>) => fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  const init = (fetchMock: ReturnType<typeof mockFetch>) => call(fetchMock)[1];

  it("reads the cart in the catalogue language, sending the cookie same-origin only", async () => {
    const fetchMock = mockFetch(jsonResponse(filled));
    await expect(getCart("de")).resolves.toEqual(filled);
    expect(call(fetchMock)[0]).toBe("/api/cart?lang=de");
    expect(init(fetchMock)).toMatchObject({ method: "GET", credentials: "same-origin", body: undefined });
  });

  it("sets a quantity with a JSON PUT to the encoded SKU", async () => {
    const fetchMock = mockFetch(jsonResponse(filled));
    await setCartQuantity("ADO-RRS-NAV-M", 2, "en");
    expect(call(fetchMock)[0]).toBe("/api/cart/items/ADO-RRS-NAV-M?lang=en");
    expect(init(fetchMock)).toMatchObject({ method: "PUT", body: JSON.stringify({ quantity: 2 }) });
    expect((init(fetchMock).headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("removes a line and releases a reservation with DELETE", async () => {
    const fetchMock = mockFetch(jsonResponse(filled));
    await removeCartLine("A B", "fr");
    expect(call(fetchMock)[0]).toBe("/api/cart/items/A%20B?lang=fr");
    expect(init(fetchMock).method).toBe("DELETE");
    mockFetch(jsonResponse(filled));
    await expect(releaseCart("en")).resolves.toEqual(filled);
  });

  it("tells a new reservation (201) from a repeated one (200)", async () => {
    const fetchMock = mockFetch(jsonResponse(reserved, 201));
    await expect(reserveCart("en")).resolves.toEqual({ cart: reserved, created: true });
    expect(call(fetchMock)[0]).toBe("/api/cart/reservation?lang=en");
    expect(init(fetchMock).method).toBe("POST");
    mockFetch(jsonResponse(reserved, 200));
    await expect(reserveCart("en")).resolves.toEqual({ cart: reserved, created: false });
  });

  it("surfaces the API's cart errors with their codes and SKUs", async () => {
    mockFetch(
      errorResponse(409, "insufficient_available_stock", "Not enough stock of X for the requested quantity.", [
        { sku: "ADO-RRS-NAV-M", requested: 11 },
      ]),
    );
    await expect(setCartQuantity("ADO-RRS-NAV-M", 11, "en")).rejects.toMatchObject({
      status: 409,
      code: "insufficient_available_stock",
      details: [{ sku: "ADO-RRS-NAV-M", requested: 11 }],
    });
  });

  it.each([
    ["a non-integer quantity", { ...filled, lines: [{ ...filled.lines[0], quantity: 1.5 }] }],
    ["a missing availability flag", { ...filled, lines: [{ ...filled.lines[0], available: undefined }] }],
    ["an unknown language", { ...filled, lines: [{ ...filled.lines[0], lang: "xx" }] }],
    ["a reservation without a valid expiry", { ...reserved, reservation: { ...reserved.reservation, expires_at: "soon" } }],
    ["a missing subtotal", { ...filled, subtotal: undefined }],
  ])("rejects a cart with %s", async (_label, body) => {
    mockFetch(jsonResponse(body));
    await expect(getCart("en")).rejects.toThrow("Unexpected cart response from the server.");
  });
});

