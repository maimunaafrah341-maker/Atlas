import { afterEach, describe, expect, it, vi } from "vitest";
import { errorResponse, jsonResponse, page, product } from "../test/fixtures.ts";
import { ApiError, getProduct, getShippingQuote, listProducts, productQueryString } from "./api.ts";

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
