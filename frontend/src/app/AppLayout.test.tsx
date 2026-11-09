import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "./catalogueLanguage.tsx";
import { routes } from "./routes.tsx";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
  return router;
}

// The homepage loads the catalogue; these shell tests only need an empty, valid page.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({ items: [], page: 1, page_size: 4, total: 0, total_pages: 0 }, { status: 200 }),
    ),
  );
});

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe("application shell", () => {
  it("states plainly that this is a fictional demo with nothing for sale", () => {
    renderAt("/");
    expect(screen.getByText(/Fictional brand demonstration — browsing only, nothing can be bought yet/)).toBeVisible();
    expect(screen.getByRole("contentinfo")).toHaveTextContent(/nothing can be purchased yet/);
  });

  it("links the five real catalogue categories in the header and footer", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav).getAllByRole("link").map((link) => [link.textContent, link.getAttribute("href")]);
    expect(links).toEqual([
      ["Shop all", "/shop"],
      ["Jackets", "/shop?category=jackets"],
      ["Base layers", "/shop?category=base_layers"],
      ["Trousers", "/shop?category=trousers"],
      ["Socks", "/shop?category=socks"],
      ["Accessories", "/shop?category=accessories"],
    ]);
    const footer = screen.getByRole("contentinfo");
    expect(within(footer).getByRole("link", { name: "System status" })).toHaveAttribute("href", "/status");
    expect(within(footer).queryByRole("textbox")).not.toBeInTheDocument(); // no newsletter without a backend
  });

  it("marks the current catalogue section", () => {
    renderAt("/shop?category=socks");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Socks" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Shop all" })).not.toHaveAttribute("aria-current");
  });

  it("shows the bag as unavailable, not as a working control", () => {
    renderAt("/");
    const bag = screen.getByText("Bag").parentElement as HTMLElement;
    expect(bag).toHaveAttribute("aria-disabled", "true");
    expect(bag).toHaveTextContent(/not available yet/);
    expect(screen.queryByRole("link", { name: /bag/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /bag/i })).not.toBeInTheDocument();
  });

  it("renders a not-found page for unknown paths", () => {
    renderAt("/nowhere");
    expect(screen.getByRole("heading", { level: 1, name: "This page isn’t on the map." })).toBeVisible();
  });
});

describe("mobile menu", () => {
  it("opens, focuses its first link, and closes with Escape", async () => {
    const user = userEvent.setup();
    renderAt("/");
    const toggle = screen.getByRole("button", { name: "Menu" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    const menu = screen.getByRole("navigation", { name: "Main menu" });
    expect(screen.getByRole("button", { name: "Close" })).toHaveAttribute("aria-expanded", "true");
    expect(within(menu).getByRole("link", { name: "Shop all" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument();
  });

  it("closes after navigating", async () => {
    const user = userEvent.setup();
    const router = renderAt("/");
    await user.click(screen.getByRole("button", { name: "Menu" }));
    await user.click(within(screen.getByRole("navigation", { name: "Main menu" })).getByRole("link", { name: "Socks" }));
    expect(router.state.location.search).toBe("?category=socks");
    expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument();
  });
});

describe("header search", () => {
  it("sends the query to the catalogue page", async () => {
    const user = userEvent.setup();
    const router = renderAt("/");
    await user.click(screen.getByRole("button", { name: "Search" }));
    const input = screen.getByRole("searchbox", { name: "Search the catalogue" });
    expect(input).toHaveFocus();
    await user.type(input, "rain jacket{Enter}");
    expect(router.state.location.pathname).toBe("/shop");
    expect(new URLSearchParams(router.state.location.search).get("q")).toBe("rain jacket");
  });
});

describe("catalogue language", () => {
  it("is labelled as affecting catalogue content only, and is remembered", async () => {
    const user = userEvent.setup();
    renderAt("/");
    const select = screen.getAllByRole("combobox", { name: /Catalogue language \(product names and descriptions\)/ })[0];
    expect(select).toHaveValue("en");
    await user.selectOptions(select as HTMLElement, "de");
    expect(select).toHaveValue("de");
    expect(window.localStorage.getItem("atlas.catalogueLanguage")).toBe("de");
  });

  it("restores a remembered language and ignores an invalid one", () => {
    window.localStorage.setItem("atlas.catalogueLanguage", "fr");
    renderAt("/");
    expect(screen.getAllByRole("combobox", { name: /Catalogue language/ })[0]).toHaveValue("fr");
  });

  it("falls back to English when the remembered value is not a supported language", () => {
    window.localStorage.setItem("atlas.catalogueLanguage", "xx");
    renderAt("/");
    expect(screen.getAllByRole("combobox", { name: /Catalogue language/ })[0]).toHaveValue("en");
  });
});
