import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "../app/catalogueLanguage.tsx";
import { routes } from "../app/routes.tsx";
import type { Variant } from "../lib/api.ts";
import { errorResponse, jsonResponse, product } from "../test/fixtures.ts";

const variant = (size: string, colour: string, stock_status: Variant["stock_status"], amount_minor = 12900): Variant => ({
  sku: `ADO-RRS-${colour.toUpperCase()}-${size}`,
  size,
  colour,
  price: { amount_minor, currency: "USD" },
  stock_status,
  matches_filters: true,
});

// Navy in S (in stock) and M (low stock, dearer); red only in M (out of stock).
const shell = product({
  price_range: { min: { amount_minor: 12900, currency: "USD" }, max: { amount_minor: 13900, currency: "USD" } },
  variants: [variant("S", "navy", "in_stock"), variant("M", "navy", "low_stock", 13900), variant("M", "red", "out_of_stock")],
});

type Route = (url: string) => Response | Promise<Response>;

function stubApi(productResponse: Route = () => jsonResponse(shell), shipping: Route = () => jsonResponse({})) {
  const fetchMock = vi.fn(async (url: string) => (url.startsWith("/api/shipping/") ? shipping(url) : productResponse(url)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderProduct(url = "/products/ridgeline-rain-shell") {
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
  return router;
}

const requested = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls.map((call) => call[0] as string);
const loaded = () => screen.findByRole("heading", { level: 1, name: "Ridgeline Rain Shell" });
const group = (name: RegExp) => within(screen.getByRole("group", { name }));
const table = () => within(screen.getByRole("table", { name: "Price and availability of each size and colour" }));

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("product page", () => {
  it("shows the live product: name, category, price range, description, tags and breadcrumb", async () => {
    const fetchMock = stubApi();
    renderProduct();
    expect(screen.getByRole("status")).toHaveTextContent("Loading product…");
    await loaded();

    expect(requested(fetchMock)).toEqual(["/api/catalog/products/ridgeline-rain-shell?lang=en"]);
    expect(document.title).toBe("Ridgeline Rain Shell · Atlas Demo Outfitters");
    expect(screen.getByText("$129.00 – $139.00")).toBeVisible();
    expect(screen.getByText(/Lightweight 2.5-layer waterproof jacket/)).toBeVisible();
    expect(screen.getByText("Breathable, Hiking, Packable, Waterproof")).toBeVisible();
    const crumbs = within(screen.getByRole("navigation", { name: "Breadcrumb" }));
    expect(crumbs.getByRole("link", { name: "Shop all" })).toHaveAttribute("href", "/shop");
    expect(crumbs.getByRole("link", { name: "Jackets" })).toHaveAttribute("href", "/shop?category=jackets");
    expect(crumbs.getByText("Ridgeline Rain Shell")).toHaveAttribute("aria-current", "page");
    expect(document.querySelectorAll("img")).toHaveLength(0);
  });

  it("lists every variant's own price and availability", async () => {
    stubApi();
    renderProduct();
    await loaded();
    const rows = table().getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      "NavyS$129.00In stockADO-RRS-NAVY-S",
      "NavyM$139.00Low stockADO-RRS-NAVY-M",
      "RedM$129.00Out of stockADO-RRS-RED-M",
    ]);
  });

  it("shows the chosen variant's own price, availability and SKU, never the product summary", async () => {
    stubApi();
    const router = renderProduct();
    await loaded();
    expect(screen.getByText("Choose a colour and size to see its availability.")).toBeVisible();

    await userEvent.click(group(/^Colour/).getByRole("radio", { name: /Navy/ }));
    await userEvent.click(group(/^Size/).getByRole("radio", { name: /^M/ }));
    expect(router.state.location.search).toBe("?size=M&colour=navy");
    expect(router.state.historyAction).toBe("REPLACE");
    expect(screen.getByText("$139.00", { selector: "p" })).toBeVisible();
    expect(screen.getByText("Low stock", { selector: "strong" }).parentElement).toHaveTextContent("Navy, M: Low stock");
    expect(screen.getByText("ADO-RRS-NAVY-M", { selector: "dd" })).toBeVisible();
    expect(table().getByText("Selected").closest("tr")).toHaveTextContent("NavySelectedM$139.00Low stockADO-RRS-NAVY-M");

    await userEvent.click(group(/^Colour/).getByRole("radio", { name: /Red/ }));
    expect(screen.getByText("Out of stock", { selector: "strong" }).parentElement).toHaveTextContent("Red, M: Out of stock");
  });

  it("only offers sizes made in the chosen colour, and clears a size the new colour lacks", async () => {
    stubApi();
    const router = renderProduct("/products/ridgeline-rain-shell?size=S&colour=navy");
    await loaded();
    expect(group(/^Size/).getByRole("radio", { name: /^S/ })).toBeChecked();

    // Red only comes in M, so S is cleared and M (the only red size) is chosen.
    await userEvent.click(group(/^Colour/).getByRole("radio", { name: /Red/ }));
    expect(router.state.location.search).toBe("?size=M&colour=red");
    expect(group(/^Size/).getByRole("radio", { name: /^S, not made in Red/ })).toBeDisabled();
    expect(group(/^Size/).getByRole("radio", { name: /^M/ })).toBeChecked();
  });

  it("notes, per colour, how it stands in the chosen size", async () => {
    stubApi();
    renderProduct("/products/ridgeline-rain-shell?size=S&colour=navy");
    await loaded();
    expect(group(/^Colour/).getByRole("radio", { name: /^Red,\s?Not in S$/ })).toBeInTheDocument();
  });

  it("moves between options with the arrow keys", async () => {
    stubApi();
    renderProduct("/products/ridgeline-rain-shell?colour=navy");
    await loaded();
    const small = group(/^Size/).getByRole("radio", { name: /^S/ });
    await userEvent.click(small);
    await userEvent.keyboard("{ArrowRight}");
    expect(group(/^Size/).getByRole("radio", { name: /^M/ })).toBeChecked();
    expect(group(/^Size/).getByRole("radio", { name: /^M/ })).toHaveFocus();
  });

  it("ignores options from the URL that the product doesn't have", async () => {
    stubApi();
    renderProduct("/products/ridgeline-rain-shell?size=XXL&colour=purple");
    await loaded();
    expect(screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked)).toHaveLength(0);
  });

  it("preselects the only colour or size a product has", async () => {
    stubApi(() =>
      jsonResponse(product({ variants: [variant("ONE_SIZE", "navy", "in_stock"), variant("ONE_SIZE", "red", "low_stock")] })),
    );
    renderProduct();
    await loaded();
    expect(group(/^Size/).getByRole("radio", { name: /One size/ })).toBeChecked();
    expect(screen.getByText("Choose a colour to see its availability.")).toBeVisible();
  });

  it("keeps Add to bag clearly unavailable", async () => {
    const fetchMock = stubApi();
    renderProduct("/products/ridgeline-rain-shell?size=S&colour=navy");
    await loaded();
    const bag = screen.getByRole("button", { name: "Add to bag" });
    expect(bag).toHaveAttribute("aria-disabled", "true");
    expect(bag).toHaveAccessibleDescription("Not available yet: the bag isn’t part of this demo, so nothing can be bought.");
    await userEvent.click(bag);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("flags machine-translated text and refetches in a new catalogue language, keeping the selection", async () => {
    const fetchMock = stubApi((url) =>
      jsonResponse(url.endsWith("lang=fr") ? { ...shell, name: "Veste de pluie Ridgeline", lang: "fr", machine_translated: true } : shell),
    );
    const router = renderProduct("/products/ridgeline-rain-shell?size=M&colour=navy");
    await loaded();
    expect(screen.queryByText("Machine-translated · not reviewed")).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getAllByRole("combobox", { name: /Catalogue language/ })[0]!, "fr");
    expect(await screen.findByRole("heading", { level: 1, name: "Veste de pluie Ridgeline" })).toBeVisible();
    expect(screen.getByText("Machine-translated · not reviewed")).toBeVisible();
    expect(requested(fetchMock).at(-1)).toBe("/api/catalog/products/ridgeline-rain-shell?lang=fr");
    expect(router.state.location.search).toBe("?size=M&colour=navy");
    expect(group(/^Size/).getByRole("radio", { name: /^M/ })).toBeChecked();
  });

  it("says when English is shown because there's no translation", async () => {
    stubApi(() => jsonResponse({ ...shell, lang: "en", fallback: true }));
    window.localStorage.setItem("atlas.catalogueLanguage", "es");
    renderProduct();
    await loaded();
    expect(screen.getByText("Shown in English · no translation yet")).toBeVisible();
  });

  it.each([
    ["a missing product", () => errorResponse(404, "product_not_found", "No product nope.")],
    ["a malformed slug", () => errorResponse(422, "invalid_request", "The request is invalid.")],
  ])("shows not-found for %s", async (_label, response) => {
    stubApi(response);
    renderProduct("/products/nope");
    expect(await screen.findByRole("heading", { level: 1, name: "This product isn’t in the catalogue." })).toBeVisible();
    expect(screen.getByRole("link", { name: "Browse the catalogue →" })).toHaveAttribute("href", "/shop");
  });

  it("offers a retry when the product can't be loaded", async () => {
    let calls = 0;
    stubApi(() => (++calls === 1 ? errorResponse(500, "internal_error", "Something went wrong on our side.") : jsonResponse(shell)));
    renderProduct();
    expect(await screen.findByRole("alert")).toHaveTextContent("This product couldn’t be loaded. Something went wrong on our side.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await loaded()).toBeVisible();
  });

  describe("shipping estimate", () => {
    const quote = { country: "DE", zone: "europe", zone_name: "Europe", price: { amount_minor: 2000, currency: "USD" } };
    const country = () => screen.getByRole("combobox", { name: "Destination country" });

    it("asks nothing until a country is chosen, then shows the API's rate", async () => {
      const fetchMock = stubApi(undefined, () => jsonResponse(quote));
      renderProduct();
      await loaded();
      expect(country()).toHaveValue("");
      expect(requested(fetchMock).some((url) => url.startsWith("/api/shipping/"))).toBe(false);

      await userEvent.selectOptions(country(), "DE");
      expect(await screen.findByText(/Shipping to Germany:/)).toHaveTextContent("Shipping to Germany: $20.00 (Europe rate)");
      expect(requested(fetchMock).at(-1)).toBe("/api/shipping/quote?country=DE");
      expect(screen.getByText("Demo rates from this fictional shop’s shipping table. Delivery times aren’t available.")).toBeVisible();
    });

    it("says plainly when the shop doesn't ship somewhere", async () => {
      stubApi(undefined, () => errorResponse(422, "unsupported_destination", "We do not ship to IN.", [{ country: "IN" }]));
      renderProduct();
      await loaded();
      await userEvent.selectOptions(country(), "IN");
      expect(await screen.findByText("This demo shop doesn’t ship to India.")).toBeVisible();
    });

    it("offers a retry when the quote fails", async () => {
      let calls = 0;
      stubApi(undefined, () => (++calls === 1 ? errorResponse(500, "internal_error", "Something went wrong on our side.") : jsonResponse(quote)));
      renderProduct();
      await loaded();
      await userEvent.selectOptions(country(), "DE");
      expect(await screen.findByText(/Shipping couldn’t be checked/)).toBeVisible();
      await userEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(await screen.findByText(/Shipping to Germany:/)).toBeVisible();
    });
  });

  it("is reached from a product card in the shop", async () => {
    stubApi((url) => (url.startsWith("/api/catalog/products?") ? jsonResponse({ items: [shell], page: 1, page_size: 12, total: 1, total_pages: 1 }) : jsonResponse(shell)));
    const router = renderProduct("/shop");
    await userEvent.click(await screen.findByRole("link", { name: "Ridgeline Rain Shell" }));
    await loaded();
    expect(router.state.location.pathname).toBe("/products/ridgeline-rain-shell");
    await act(() => router.navigate(-1));
    expect(router.state.location.pathname).toBe("/shop");
  });
});
