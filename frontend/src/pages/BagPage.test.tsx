import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "../app/catalogueLanguage.tsx";
import { routes } from "../app/routes.tsx";
import { fakeCartServer, type FakeCartServer } from "../test/fakeCartServer.ts";
import { jsonResponse, page } from "../test/fixtures.ts";

function renderBag(server: FakeCartServer, path = "/bag") {
  vi.stubGlobal("fetch", vi.fn(server.fetch));
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
  return router;
}

const lineFor = (name: RegExp) => screen.getByRole("heading", { level: 3, name }).closest("article") as HTMLElement;
const summary = () => within(screen.getByRole("complementary", { name: "Summary" }));
const shell = /Ridgeline Rain Shell|Ridgeline Regenjacke/;

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("bag page", () => {
  it("shows an empty bag with a way back to the catalogue", async () => {
    renderBag(fakeCartServer());
    expect(screen.getByRole("status", { name: "Loading your bag" })).toBeInTheDocument();
    expect(await screen.findByText("Your bag is empty.")).toBeVisible();
    expect(screen.getByRole("link", { name: /Explore the catalogue/ })).toHaveAttribute("href", "/shop");
  });

  it("lists lines with options, prices and the server's totals", async () => {
    const server = fakeCartServer([
      { sku: "ADO-RRS-NAV-M", quantity: 2 },
      { sku: "SOCK-GRY-M", quantity: 3 },
    ]);
    server.subtotalOverride = 4321; // proves the subtotal is the server's, not added up here
    renderBag(server);

    await screen.findByRole("complementary", { name: "Summary" });
    const jacket = lineFor(shell);
    expect(within(jacket).getByRole("link", { name: "Ridgeline Rain Shell" })).toHaveAttribute(
      "href",
      "/products/ridgeline-rain-shell?size=M&colour=navy",
    );
    expect(jacket).toHaveTextContent("Navy · M");
    expect(jacket).toHaveTextContent("$129.00 each");
    expect(jacket).toHaveTextContent("Line total: $258.00");
    expect(within(jacket).getByRole("group", { name: /Quantity of Ridgeline Rain Shell/ })).toHaveTextContent("2");
    expect(lineFor(/Merino Hiking Socks/)).toHaveTextContent("Line total: $72.00");

    expect(summary().getByText("Items").nextElementSibling).toHaveTextContent("5");
    expect(summary().getByText("Subtotal").nextElementSibling).toHaveTextContent("$43.21");
    expect(screen.getByRole("link", { name: "Bag, 5 items" })).toBeVisible();
  });

  it("changes quantities and removes lines through the API, showing what the server returns", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }, { sku: "SOCK-GRY-M", quantity: 1 }]);
    renderBag(server);
    const jacket = await screen.findByRole("group", { name: /Quantity of Ridgeline Rain Shell/ });

    const decrease = within(jacket).getByRole("button", { name: /Decrease/ });
    expect(decrease).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(decrease);
    expect(server.calls.filter((call) => call.startsWith("PUT"))).toEqual([]);

    await userEvent.click(within(jacket).getByRole("button", { name: /Increase/ }));
    await waitFor(() => expect(jacket).toHaveTextContent("2"));
    expect(server.calls).toContain("PUT /api/cart/items/ADO-RRS-NAV-M?lang=en");
    expect(summary().getByText("Subtotal").nextElementSibling).toHaveTextContent("$282.00");

    await userEvent.click(screen.getByRole("button", { name: /Remove Merino Hiking Socks/ }));
    await waitFor(() => expect(screen.queryByText("Merino Hiking Socks")).not.toBeInTheDocument());
    expect(server.calls).toContain("DELETE /api/cart/items/SOCK-GRY-M?lang=en");

    await userEvent.click(screen.getByRole("button", { name: /Remove Ridgeline Rain Shell/ }));
    expect(await screen.findByText("Your bag is empty.")).toBeVisible();
  });

  it("reserves with a countdown, then releases", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }]);
    renderBag(server);
    await userEvent.click(await screen.findByRole("button", { name: "Reserve items" }));

    const timer = await summary().findByRole("timer");
    expect(timer.textContent).toMatch(/^1[45]:\d\d$/);
    expect(summary().getByText("Reserved")).toBeVisible();
    expect(server.calls).toContain("POST /api/cart/reservation?lang=en");

    await userEvent.click(summary().getByRole("button", { name: "Release reservation" }));
    expect(await summary().findByText("Your reservation was released.")).toBeVisible();
    expect(summary().getByRole("button", { name: "Reserve items" })).toBeVisible();
    expect(server.calls).toContain("DELETE /api/cart/reservation?lang=en");
  });

  it("says when an edit releases the reservation", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }]);
    renderBag(server);
    await userEvent.click(await screen.findByRole("button", { name: "Reserve items" }));
    await summary().findByRole("timer");

    await userEvent.click(screen.getByRole("button", { name: /Increase quantity of Ridgeline/ }));
    expect(await summary().findByText(/Your bag changed, so the reservation was released/)).toBeVisible();
    expect(summary().queryByRole("timer")).not.toBeInTheDocument();
  });

  it("re-reads the cart when the reservation runs out and says it expired", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }]);
    server.ttlMs = 1200;
    renderBag(server);
    await userEvent.click(await screen.findByRole("button", { name: "Reserve items" }));
    await summary().findByRole("timer");

    expect(
      await summary().findByText(/Your reservation expired and the stock was released/, undefined, { timeout: 4000 }),
    ).toBeVisible();
    expect(summary().getByRole("button", { name: "Reserve items" })).toBeVisible();
    expect(server.calls.filter((call) => call === "GET /api/cart?lang=en").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Ridgeline Rain Shell")).toBeVisible(); // the items stay
  });

  it("marks unavailable lines and explains why reserving fails", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }, { sku: "SOCK-GRY-M", quantity: 2 }]);
    server.archive("SOCK-GRY-M");
    renderBag(server);
    const socks = await waitFor(() => lineFor(/Merino Hiking Socks/));
    expect(socks).toHaveTextContent("No longer available");
    expect(within(socks).getByRole("button", { name: /Increase/ })).toHaveAttribute("aria-disabled", "true");
    expect(summary().getByText("Subtotal").nextElementSibling).toHaveTextContent("$129.00");

    await userEvent.click(summary().getByRole("button", { name: "Reserve items" }));
    expect(await summary().findByRole("alert")).toHaveTextContent("Some items are no longer available. Remove them, then try again.");
  });

  it("flags lines that are short of stock and re-reads the cart", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }]);
    renderBag(server);
    await screen.findByRole("group", { name: /Quantity of Ridgeline/ });
    server.failNext({
      kind: "api",
      status: 409,
      code: "insufficient_available_stock",
      details: [{ sku: "ADO-RRS-NAV-M", requested: 2 }],
    });
    const before = server.calls.length;
    await userEvent.click(screen.getByRole("button", { name: /Increase quantity of Ridgeline/ }));

    expect(await summary().findByRole("alert")).toHaveTextContent("There isn’t enough stock for that quantity.");
    expect(summary().queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(lineFor(shell)).toHaveTextContent("Not enough stock for this quantity.");
    await waitFor(() => expect(server.calls.slice(before)).toContain("GET /api/cart?lang=en"));
  });

  it.each([
    ["reservation_conflict", "Your bag changed while it was being reserved. Please try again."],
    ["cart_not_found", "Your bag has expired. Add items to start a new one."],
  ])("explains %s and re-reads the cart", async (code, message) => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }]);
    renderBag(server);
    await screen.findByRole("button", { name: "Reserve items" });
    server.failNext({ kind: "api", status: code === "cart_not_found" ? 404 : 409, code }, "POST");
    const before = server.calls.length;
    await userEvent.click(summary().getByRole("button", { name: "Reserve items" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    await waitFor(() => expect(server.calls.slice(before)).toContain("GET /api/cart?lang=en"));
  });

  it("offers a retry when the bag can't be loaded", async () => {
    const server = fakeCartServer([{ sku: "SOCK-GRY-M", quantity: 1 }]);
    server.failNext({ kind: "network" });
    renderBag(server);
    expect(await screen.findByRole("alert")).toHaveTextContent("Your bag couldn’t be loaded. Could not reach the server.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Merino Hiking Socks")).toBeVisible();
  });

  it("retries a change that failed for lack of a connection", async () => {
    const server = fakeCartServer([{ sku: "SOCK-GRY-M", quantity: 1 }]);
    renderBag(server);
    await screen.findByText("Merino Hiking Socks");
    server.failNext({ kind: "network" }, "PUT");
    await userEvent.click(screen.getByRole("button", { name: /Increase quantity of Merino/ }));
    const alert = await summary().findByRole("alert");
    expect(alert).toHaveTextContent("Could not reach the server.");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(summary().getByText("Items").nextElementSibling).toHaveTextContent("2"));
    expect(server.calls.filter((call) => call.startsWith("PUT"))).toHaveLength(2);
  });

  it("ignores extra clicks while a change is in flight", async () => {
    const server = fakeCartServer([{ sku: "SOCK-GRY-M", quantity: 1 }]);
    renderBag(server);
    await screen.findByText("Merino Hiking Socks");
    const release = server.hold();
    const increase = screen.getByRole("button", { name: /Increase quantity of Merino/ });
    await userEvent.click(increase);
    await waitFor(() => expect(increase).toHaveAttribute("aria-disabled", "true"));
    await userEvent.click(increase);
    await userEvent.click(increase);
    act(() => release());
    await waitFor(() => expect(summary().getByText("Items").nextElementSibling).toHaveTextContent("2"));
    expect(server.calls.filter((call) => call.startsWith("PUT"))).toHaveLength(1);
    expect(increase).toHaveFocus(); // aria-disabled, not disabled: keyboard focus stays put
  });

  it("shows names in the catalogue language with translation notes, and refetches on change", async () => {
    const server = fakeCartServer([{ sku: "ADO-RRS-NAV-M", quantity: 1 }, { sku: "SOCK-GRY-M", quantity: 1 }]);
    renderBag(server);
    await screen.findByText("Ridgeline Rain Shell");
    await userEvent.selectOptions(screen.getAllByRole("combobox", { name: /Catalogue language/ })[0]!, "de");
    expect(await screen.findByText("Ridgeline Regenjacke")).toBeVisible();
    expect(lineFor(/Ridgeline Regenjacke/)).toHaveTextContent("Machine-translated · not reviewed");
    expect(lineFor(/Merino Hiking Socks/)).toHaveTextContent("Shown in English · no translation yet");
    expect(server.calls).toContain("GET /api/cart?lang=de");
  });

  it("keeps the bag across navigation without reading or storing any token", async () => {
    const cookieRead = vi.spyOn(document, "cookie", "get");
    const server = fakeCartServer([{ sku: "SOCK-GRY-M", quantity: 2 }]);
    const fallback = server.fetch;
    server.fetch = async (url, init) =>
      url.startsWith("/api/catalog") ? jsonResponse(page([])) : fallback(url, init);
    const router = renderBag(server);
    await screen.findByText("Merino Hiking Socks");

    await act(() => router.navigate("/shop"));
    expect(screen.getByRole("link", { name: "Bag, 2 items" })).toBeVisible();
    await act(() => router.navigate("/bag"));
    expect(await screen.findByText("Merino Hiking Socks")).toBeVisible();

    expect(cookieRead).not.toHaveBeenCalled();
    expect(Object.keys(window.localStorage)).toEqual([]);
    expect(server.calls.filter((call) => call.startsWith("GET /api/cart"))).toHaveLength(1); // read once, kept in memory
  });
});
