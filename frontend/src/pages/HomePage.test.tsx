import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "../app/catalogueLanguage.tsx";
import { routes } from "../app/routes.tsx";
import { errorResponse, jsonResponse, page, product, withCart } from "../test/fixtures.ts";

function renderHome() {
  const router = createMemoryRouter(routes, { initialEntries: ["/"] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
}

const products = [
  product(),
  product({
    slug: "trail-merino-tee",
    category: "base_layers",
    name: "Camiseta de merino Trail",
    lang: "es",
    machine_translated: true,
    price_range: { min: { amount_minor: 5900, currency: "USD" }, max: { amount_minor: 6900, currency: "USD" } },
    variants: [{ ...product().variants[0]!, stock_status: "out_of_stock" }],
  }),
];

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("homepage", () => {
  it("links all five real catalogue categories", () => {
    vi.stubGlobal("fetch", withCart(vi.fn(async () => jsonResponse(page([])))));
    renderHome();
    const grid = screen.getByRole("heading", { name: "Equipment, organised by use." }).closest("section") as HTMLElement;
    // Accessible names: the decorative placeholders and arrows are hidden from assistive technology.
    expect(within(grid).getAllByRole("link")).toHaveLength(5);
    for (const [name, href] of [
      ["Jackets", "/shop?category=jackets"],
      ["Base layers", "/shop?category=base_layers"],
      ["Trousers", "/shop?category=trousers"],
      ["Socks", "/shop?category=socks"],
      ["Accessories", "/shop?category=accessories"],
    ]) {
      expect(within(grid).getByRole("link", { name })).toHaveAttribute("href", href);
    }
  });

  it("shows selected equipment from the API: names, prices, stock and translation notes", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(page(products, { total: 20, page_size: 4 })));
    vi.stubGlobal("fetch", withCart(fetchMock));
    window.localStorage.setItem("atlas.catalogueLanguage", "es");
    renderHome();

    expect(screen.getByRole("status")).toHaveTextContent("Loading products…");
    const shell = await screen.findByRole("link", { name: "Ridgeline Rain Shell" });
    expect(shell).toHaveAttribute("href", "/products/ridgeline-rain-shell");
    expect(screen.getByText("Current catalogue / 20 products")).toBeVisible();

    const [first, second] = screen.getAllByRole("article");
    expect(first).toHaveTextContent("$129.00");
    expect(first).toHaveTextContent("In stock");
    expect(first).not.toHaveTextContent("Machine-translated");
    expect(second).toHaveTextContent("$59.00 – $69.00");
    expect(second).toHaveTextContent("Out of stock");
    expect(second).toHaveTextContent("Machine-translated · not reviewed");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/catalog/products?lang=es&sort=featured&page_size=4",
      expect.anything(),
    );
  });

  it("refetches in the newly chosen catalogue language", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(page(products)));
    vi.stubGlobal("fetch", withCart(fetchMock));
    renderHome();
    await screen.findByRole("link", { name: "Ridgeline Rain Shell" });
    await userEvent.selectOptions(screen.getAllByRole("combobox", { name: /Catalogue language/ })[0]!, "de");
    await vi.waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith("/api/catalog/products?lang=de&sort=featured&page_size=4", expect.anything()),
    );
  });

  it("offers a retry when the catalogue can't be loaded", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(errorResponse(500, "internal_error", "Something went wrong on our side."))
      .mockResolvedValueOnce(jsonResponse(page(products)));
    vi.stubGlobal("fetch", withCart(fetchMock));
    renderHome();

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalogue couldn’t be loaded.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Ridgeline Rain Shell" })).toBeVisible();
  });

  it("says so when the catalogue is empty", async () => {
    vi.stubGlobal("fetch", withCart(vi.fn(async () => jsonResponse(page([])))));
    renderHome();
    expect(await screen.findByText("The catalogue is empty right now.")).toBeVisible();
  });

  it("presents the assistant only as a non-functional concept", () => {
    vi.stubGlobal("fetch", withCart(vi.fn(async () => jsonResponse(page([])))));
    renderHome();
    const teaser = screen.getByRole("heading", { name: "A quieter way to find the right equipment." }).closest("section") as HTMLElement;
    expect(teaser).toHaveTextContent("Concept · not available yet");
    expect(teaser).toHaveTextContent("nothing here is powered by AI yet");
    expect(within(teaser).queryByRole("button")).not.toBeInTheDocument();
    expect(within(teaser).queryByRole("link")).not.toBeInTheDocument();
    expect(within(teaser).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("uses neutral placeholders, not images", () => {
    vi.stubGlobal("fetch", withCart(vi.fn(async () => jsonResponse(page([])))));
    renderHome();
    expect(document.querySelectorAll("img")).toHaveLength(0);
  });
});
