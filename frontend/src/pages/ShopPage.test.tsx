import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "../app/catalogueLanguage.tsx";
import { routes } from "../app/routes.tsx";
import { errorResponse, jsonResponse, page, product } from "../test/fixtures.ts";

const API = "/api/catalog/products";

function renderShop(url = "/shop") {
  const router = createMemoryRouter(routes, { initialEntries: [url] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
  return router;
}

function stubCatalogue(body = page([product()])) {
  const fetchMock = vi.fn(async () => jsonResponse(body));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const requested = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls.map((call) => call[0] as string);
const search = (router: ReturnType<typeof renderShop>) => router.state.location.search;
const filters = () => within(screen.getByRole("complementary", { name: "Filters" }));
const loaded = () => screen.findByRole("link", { name: "Ridgeline Rain Shell" });

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("shop page", () => {
  it("lists live products with their count, price, stock and translation notes", async () => {
    stubCatalogue(
      page(
        [
          product(),
          product({ slug: "trail-tee", name: "Trail Tee", lang: "de", machine_translated: true, variants: [{ ...product().variants[0]!, stock_status: "out_of_stock" }] }),
        ],
        { total: 2 },
      ),
    );
    renderShop();
    expect(screen.getByRole("heading", { level: 1, name: "Shop all" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Loading products…");
    expect(await loaded()).toHaveAttribute("href", "/products/ridgeline-rain-shell");
    expect(screen.getByText("2 products")).toBeVisible();
    const [first, second] = screen.getAllByRole("article");
    expect(first).toHaveTextContent("Jackets");
    expect(first).toHaveTextContent("$129.00");
    expect(first).toHaveTextContent("In stock");
    expect(second).toHaveTextContent("Out of stock");
    expect(second).toHaveTextContent("Machine-translated · not reviewed");
    expect(screen.queryByRole("button", { name: /add|bag|cart/i })).not.toBeInTheDocument();
  });

  it("initialises from the URL: request, controls and active-filter chips", async () => {
    const fetchMock = stubCatalogue(page([product()], { total: 30, total_pages: 3, page: 2, page_size: 12 }));
    renderShop("/shop?category=jackets&size=M&colour=navy&min_price=50&max_price=150&in_stock=1&sort=price_desc&page=2");
    await loaded();

    expect(requested(fetchMock)).toEqual([
      `${API}?lang=en&category=jackets&size=M&colour=navy&min_price_minor=5000&max_price_minor=15000&in_stock=true&sort=price_desc&page=2&page_size=12`,
    ]);
    expect(screen.getByRole("heading", { level: 1, name: "Jackets" })).toBeVisible();
    expect(filters().getByRole("checkbox", { name: "Jackets" })).toBeChecked();
    expect(filters().getByRole("checkbox", { name: "Socks" })).not.toBeChecked();
    expect(filters().getByRole("checkbox", { name: "M" })).toBeChecked();
    expect(filters().getByRole("checkbox", { name: "Navy" })).toBeChecked();
    expect(filters().getByRole("checkbox", { name: "In stock only" })).toBeChecked();
    expect(filters().getByRole("textbox", { name: "Minimum" })).toHaveValue("50");
    expect(screen.getByRole("combobox", { name: "Sort" })).toHaveValue("price_desc");
    expect(screen.getByText("30 products · Page 2 of 3")).toBeVisible();

    const chips = within(screen.getByRole("group", { name: "Active filters" }));
    for (const name of ["Jackets", "Size M", "Navy", "$50 – $150", "In stock only"]) {
      expect(chips.getByRole("button", { name: `Remove filter: ${name}` })).toBeVisible();
    }
    expect(screen.getByRole("button", { name: "Filters (5 active)" })).toBeVisible();
  });

  it("applies a filter: new URL, page reset, one request, and back undoes it", async () => {
    const fetchMock = stubCatalogue(page([product()], { total: 30, total_pages: 3 }));
    const router = renderShop("/shop?size=M&page=3");
    await loaded();

    await userEvent.click(filters().getByRole("checkbox", { name: "Socks" }));
    expect(search(router)).toBe("?category=socks&size=M");
    await loaded();
    expect(requested(fetchMock)).toEqual([
      `${API}?lang=en&size=M&page=3&page_size=12`,
      `${API}?lang=en&category=socks&size=M&page=1&page_size=12`,
    ]);

    await act(() => router.navigate(-1));
    expect(search(router)).toBe("?size=M&page=3");
    expect(filters().getByRole("checkbox", { name: "Socks" })).not.toBeChecked();
    await loaded();
    expect(requested(fetchMock).at(-1)).toBe(`${API}?lang=en&size=M&page=3&page_size=12`);

    await act(() => router.navigate(1));
    expect(filters().getByRole("checkbox", { name: "Socks" })).toBeChecked();
  });

  it("removes a single filter from its chip and clears them all", async () => {
    stubCatalogue();
    const router = renderShop("/shop?category=jackets&colour=navy&in_stock=1");
    await loaded();
    await userEvent.click(screen.getByRole("button", { name: "Remove filter: Navy" }));
    expect(search(router)).toBe("?category=jackets&in_stock=1");
    await userEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(search(router)).toBe("");
    expect(screen.queryByRole("group", { name: "Active filters" })).not.toBeInTheDocument();
  });

  it("searches, offers best-match sorting only while searching, and sorts", async () => {
    const fetchMock = stubCatalogue();
    const router = renderShop("/shop?page=2");
    await loaded();
    const sort = screen.getByRole("combobox", { name: "Sort" });
    expect(within(sort).queryByRole("option", { name: "Best match" })).not.toBeInTheDocument();

    const box = screen.getByRole("searchbox", { name: "Search the catalogue" });
    await userEvent.type(box, "rain shell{Enter}");
    expect(search(router)).toBe("?q=rain+shell");
    await loaded();
    expect(requested(fetchMock).at(-1)).toBe(`${API}?q=rain+shell&lang=en&page=1&page_size=12`);
    expect(screen.getByRole("combobox", { name: "Sort" })).toHaveValue("relevance");
    expect(screen.getByText("1 product for “rain shell”")).toBeVisible();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Sort" }), "price_asc");
    expect(search(router)).toBe("?q=rain+shell&sort=price_asc");
    await loaded();
    expect(requested(fetchMock).at(-1)).toBe(`${API}?q=rain+shell&lang=en&sort=price_asc&page=1&page_size=12`);

    await userEvent.click(screen.getByRole("button", { name: "Remove search: “rain shell”" }));
    expect(search(router)).toBe("?sort=price_asc");
    expect(screen.getByRole("searchbox", { name: "Search the catalogue" })).toHaveValue("");
  });

  it("rejects a search the API can't answer, without sending it", async () => {
    const fetchMock = stubCatalogue();
    const router = renderShop();
    await loaded();
    await userEvent.type(screen.getByRole("searchbox", { name: "Search the catalogue" }), "a{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent("Search for a word of at least two letters or numbers.");
    expect(search(router)).toBe("");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("validates and applies the price range", async () => {
    const fetchMock = stubCatalogue();
    const router = renderShop("/shop?page=2");
    await loaded();
    const min = filters().getByRole("textbox", { name: "Minimum" });
    const max = filters().getByRole("textbox", { name: "Maximum" });

    await userEvent.type(min, "200");
    await userEvent.type(max, "100");
    await userEvent.click(filters().getByRole("button", { name: "Apply" }));
    expect(filters().getByRole("alert")).toHaveTextContent("The minimum can’t be more than the maximum.");
    expect(min).toHaveAttribute("aria-invalid", "true");
    expect(search(router)).toBe("?page=2");

    await userEvent.clear(max);
    await userEvent.type(max, "12.5");
    await userEvent.click(filters().getByRole("button", { name: "Apply" }));
    expect(filters().getByRole("alert")).toHaveTextContent("Enter whole dollars");

    await userEvent.clear(max);
    await userEvent.type(max, "300{Enter}");
    expect(search(router)).toBe("?min_price=200&max_price=300");
    await loaded();
    expect(requested(fetchMock).at(-1)).toBe(`${API}?lang=en&min_price_minor=20000&max_price_minor=30000&page=1&page_size=12`);
  });

  it("paginates with real links and moves focus to the results", async () => {
    stubCatalogue(page([product()], { total: 30, total_pages: 3, page_size: 12 }));
    const router = renderShop("/shop?category=socks");
    await loaded();
    const nav = within(screen.getByRole("navigation", { name: "Pagination" }));
    expect(nav.getByRole("link", { name: "Page 1" })).toHaveAttribute("aria-current", "page");
    expect(nav.getByRole("link", { name: "Page 3" })).toHaveAttribute("href", "/shop?category=socks&page=3");

    await userEvent.click(nav.getByRole("link", { name: /Next/ }));
    expect(search(router)).toBe("?category=socks&page=2");
    expect(screen.getByRole("heading", { name: "Results" })).toHaveFocus();
    await loaded();
    expect(within(screen.getByRole("navigation", { name: "Pagination" })).getByRole("link", { name: /Previous/ })).toHaveAttribute(
      "href",
      "/shop?category=socks",
    );
  });

  it("explains a page past the end and links to the last page", async () => {
    stubCatalogue(page([], { total: 20, total_pages: 2, page: 9 }));
    renderShop("/shop?page=9");
    expect(await screen.findByText("There’s no page 9. These results end at page 2.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Go to page 2" })).toHaveAttribute("href", "/shop?page=2");
  });

  it("shows an empty result with a way to reset the filters", async () => {
    stubCatalogue(page([]));
    const router = renderShop("/shop?size=XS&colour=white");
    expect(await screen.findByText("No products match these filters. Try removing one, or start again.")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Clear all filters" }));
    expect(search(router)).toBe("");
  });

  it("offers a retry when the catalogue can't be loaded", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(errorResponse(500, "internal_error", "Something went wrong on our side."))
      .mockResolvedValueOnce(jsonResponse(page([product()])));
    vi.stubGlobal("fetch", fetchMock);
    renderShop("/shop?category=jackets");
    expect(await screen.findByRole("alert")).toHaveTextContent("The catalogue couldn’t be loaded. Something went wrong on our side.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await loaded();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(`${API}?lang=en&category=jackets&page=1&page_size=12`);
  });

  it("explains a request the API rejects and offers a reset instead of a retry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => errorResponse(422, "invalid_request", "Invalid value for q: must contain a word of at least 2 letters or digits")),
    );
    const router = renderShop("/shop?q=a");
    expect(await screen.findByRole("alert")).toHaveTextContent("These filters can’t be applied. Invalid value for q");
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear all filters" }));
    expect(search(router)).toBe("");
  });

  it("refetches the same filters in a newly chosen catalogue language", async () => {
    const fetchMock = stubCatalogue();
    renderShop("/shop?category=socks&page=2");
    await loaded();
    await userEvent.selectOptions(screen.getAllByRole("combobox", { name: /Catalogue language/ })[0]!, "pt-BR");
    await vi.waitFor(() =>
      expect(requested(fetchMock)).toEqual([
        `${API}?lang=en&category=socks&page=2&page_size=12`,
        `${API}?lang=pt-BR&category=socks&page=2&page_size=12`,
      ]),
    );
  });

  it("opens and closes the filter panel on small screens, returning focus", async () => {
    stubCatalogue();
    renderShop();
    await loaded();
    const toggle = screen.getByRole("button", { name: "Filters" });
    const panel = screen.getByRole("complementary", { name: "Filters" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", panel.id);
    expect(panel).toHaveClass("hidden", "lg:block");

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).not.toHaveClass("hidden");

    // Filters apply immediately; the panel stays open for more choices.
    await userEvent.click(within(panel).getByRole("checkbox", { name: "Trousers" }));
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Filters (1 active)" })).toBeVisible();

    await userEvent.keyboard("{Escape}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();

    await userEvent.click(toggle);
    await loaded();
    await userEvent.click(within(panel).getByRole("button", { name: "Show 1 product" }));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
  });

  it("follows the header search and keeps the navigation's current category", async () => {
    stubCatalogue();
    const router = renderShop("/shop?category=socks");
    await loaded();
    const nav = within(screen.getAllByRole("navigation", { name: "Main" })[0]!);
    expect(nav.getByRole("link", { name: "Socks" })).toHaveAttribute("aria-current", "page");

    await act(() => router.navigate("/shop?category=socks&category=jackets"));
    expect(nav.getByRole("link", { name: "Socks" })).not.toHaveAttribute("aria-current");

    await act(() => router.navigate("/shop?q=merino"));
    expect(screen.getByRole("searchbox", { name: "Search the catalogue" })).toHaveValue("merino");
  });

  describe("card availability under variant filters", () => {
    // In stock in size S only; the size M filter matches just the out-of-stock M.
    const sizeSplit = product({
      variants: [
        { ...product().variants[0]!, size: "S", stock_status: "in_stock", matches_filters: false },
        { ...product().variants[1]!, size: "M", stock_status: "out_of_stock", matches_filters: true },
      ],
    });
    const card = async () => {
      await loaded();
      return screen.getByRole("article");
    };

    it("summarises only the matching variants when a size, colour, price or stock filter is active", async () => {
      stubCatalogue(page([sizeSplit]));
      renderShop("/shop?size=M");
      const article = await card();
      expect(article).toHaveTextContent("Out of stock · matching options");
      expect(article).not.toHaveTextContent("In stock");
    });

    it("keeps the product-wide summary without variant filters (category and search don't filter variants)", async () => {
      stubCatalogue(page([{ ...sizeSplit, variants: sizeSplit.variants.map((v) => ({ ...v, matches_filters: true })) }]));
      renderShop("/shop?category=jackets&q=shell");
      const article = await card();
      expect(article).toHaveTextContent("In stock");
      expect(article).not.toHaveTextContent("matching options");
    });

    it("falls back to the product-wide summary if no variant is flagged as matching", async () => {
      stubCatalogue(page([{ ...sizeSplit, variants: sizeSplit.variants.map((v) => ({ ...v, matches_filters: false })) }]));
      renderShop("/shop?in_stock=1");
      const article = await card();
      expect(article).toHaveTextContent("In stock");
      expect(article).not.toHaveTextContent("matching options");
    });
  });
});
