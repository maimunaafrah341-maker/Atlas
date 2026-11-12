import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "../../app/catalogueLanguage.tsx";
import { routes } from "../../app/routes.tsx";
import type { Cart, Order } from "../../lib/api.ts";
import { cart, cartLine, errorResponse, jsonResponse } from "../../test/fixtures.ts";
import type { PayPalButtonOptions } from "./paypal.ts";

const REF = "4c11adc21e6e4cd19b0ca4c938c15a2a";
const HASH = "a".repeat(64);
const usd = (amount_minor: number) => ({ amount_minor, currency: "USD" });

function order(status = "pending_approval"): Order {
  return {
    ref: REF,
    status,
    version_hash: HASH,
    lines: [{ sku: "ADO-RRS-NAV-M", product_slug: "ridgeline-rain-shell", name: "Ridgeline Rain Shell", size: "M", colour: "navy", quantity: 1, unit_price: usd(12900), line_total: usd(12900) }],
    item_count: 1,
    subtotal: usd(12900),
    shipping: { country: "FR", zone: "europe", price: usd(2000) },
    total: usd(14900),
    hold_expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
    created_at: new Date().toISOString(),
  };
}

/** A stateful stand-in for the cart and checkout API. */
function fakeShop({ paymentsEnabled = true, capture = "paid" }: { paymentsEnabled?: boolean; capture?: "paid" | "recoverable" } = {}) {
  const state = { ordered: false, paid: false, cancelled: false, calls: [] as { method: string; url: string; body: unknown }[] };
  const reserved = (): Cart =>
    cart({
      lines: [cartLine()],
      item_count: 1,
      subtotal: usd(12900),
      reservation: state.ordered ? null : { id: "res-1", status: "active", expires_at: new Date(Date.now() + 15 * 60_000).toISOString(), items: [{ sku: "ADO-RRS-NAV-M", quantity: 1 }] },
      checkout: state.ordered ? { order_ref: REF, status: "pending_approval" } : null,
    });
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    state.calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null });
    const path = url.split("?")[0] ?? "";
    if (path === "/api/cart" && method === "GET") return jsonResponse(state.paid ? cart() : reserved());
    if (path === "/api/checkout/config")
      return jsonResponse({ payments_enabled: paymentsEnabled, paypal_client_id: paymentsEnabled ? "sb-client" : null, currency: "USD", intent: "capture" });
    if (path === "/api/cart/orders" && method === "POST") {
      const country = (state.calls.at(-1)?.body as { ship_country: string }).ship_country;
      if (country === "AQ") return errorResponse(422, "unsupported_destination", "We don’t ship to that destination.");
      state.ordered = true;
      return jsonResponse(order(), 201);
    }
    if (path === `/api/cart/orders/${REF}` && method === "GET") return jsonResponse(order());
    if (path === `/api/cart/orders/${REF}/paypal-order`) return jsonResponse({ paypal_order_id: "PAYPAL-ORDER-1", order: order("awaiting_payment") });
    if (path === `/api/cart/orders/${REF}/capture`) {
      if (capture === "recoverable") return jsonResponse({ result: "instrument_declined", recoverable: true, order: order("awaiting_payment") });
      state.paid = true;
      return jsonResponse({ result: "paid", recoverable: false, order: order("paid") });
    }
    if (path === `/api/cart/orders/${REF}/cancel`) {
      state.ordered = false;
      state.cancelled = true;
      return jsonResponse(order("cancelled"));
    }
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return state;
}

/** PayPal's SDK: one "PayPal" button that runs createOrder then onApprove, like a buyer approving. */
function fakePayPal() {
  const restart = vi.fn(async () => undefined);
  window.paypal = {
    Buttons: (options: PayPalButtonOptions) => ({
      render: async (container: HTMLElement) => {
        const button = document.createElement("button");
        button.textContent = "PayPal";
        button.onclick = async () => {
          const id = await options.createOrder();
          await options.onApprove({ orderID: id }, { restart });
        };
        container.appendChild(button);
      },
      close: async () => undefined,
    }),
  };
  return { restart };
}

function renderBag() {
  const router = createMemoryRouter(routes, { initialEntries: ["/bag"] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
}

const summary = () => within(screen.getByRole("complementary", { name: "Summary" }));

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.paypal;
  window.localStorage.clear();
});

describe("checkout", () => {
  it("reviews the server's order and pays with PayPal Sandbox, end to end", async () => {
    const shop = fakeShop();
    fakePayPal();
    renderBag();
    await screen.findByRole("complementary", { name: "Summary" });
    const user = userEvent.setup();
    await user.selectOptions(await summary().findByRole("combobox", { name: "Ship to" }), "FR");
    await user.click(summary().getByRole("button", { name: "Review order" }));

    expect(await summary().findByText("Order review")).toBeVisible();
    expect(summary().getByText("Shipping to France")).toBeVisible();
    expect(summary().getByText("$149.00")).toBeVisible();

    await user.click(await summary().findByRole("button", { name: "PayPal" }));
    const receipt = await screen.findByRole("region", { name: "Thank you, your order is paid." });
    expect(receipt).toHaveTextContent(REF);
    expect(receipt).toHaveTextContent("$149.00");
    expect(screen.queryByText("Your bag is empty.")).not.toBeInTheDocument(); // the receipt replaces it

    const sent = (suffix: string) => shop.calls.find((call) => (call.url.split("?")[0] ?? "").endsWith(suffix));
    expect(sent("/api/cart/orders")?.body).toEqual({ ship_country: "FR" });
    expect(sent("/paypal-order")?.body).toEqual({ version_hash: HASH });
    expect(sent("/capture")?.body).toEqual({ paypal_order_id: "PAYPAL-ORDER-1" });
  });

  it("restarts PayPal when the payment method is declined, keeping the order", async () => {
    fakeShop({ capture: "recoverable" });
    const { restart } = fakePayPal();
    renderBag();
    await screen.findByRole("complementary", { name: "Summary" });
    const user = userEvent.setup();
    await user.click(await summary().findByRole("button", { name: "Review order" }));
    await user.click(await summary().findByRole("button", { name: "PayPal" }));
    expect(await summary().findByRole("alert")).toHaveTextContent("PayPal couldn’t use that payment method");
    expect(restart).toHaveBeenCalledOnce();
    expect(screen.queryByRole("region", { name: /order is paid/ })).not.toBeInTheDocument();
  });

  it("can cancel the order, releasing it", async () => {
    const shop = fakeShop();
    fakePayPal();
    renderBag();
    await screen.findByRole("complementary", { name: "Summary" });
    const user = userEvent.setup();
    await user.click(await summary().findByRole("button", { name: "Review order" }));
    await user.click(await summary().findByRole("button", { name: "Cancel order" }));
    expect(await summary().findByRole("button", { name: "Review order" })).toBeVisible();
    expect(shop.cancelled).toBe(true);
  });

  it("says so when payments aren't enabled on the server", async () => {
    fakeShop({ paymentsEnabled: false });
    renderBag();
    await screen.findByRole("complementary", { name: "Summary" });
    const user = userEvent.setup();
    await user.click(await summary().findByRole("button", { name: "Review order" }));
    expect(await summary().findByText("PayPal Sandbox payments aren’t enabled on this server.")).toBeVisible();
  });

  it("shows the server's error for an unsupported destination", async () => {
    fakeShop();
    renderBag();
    await screen.findByRole("complementary", { name: "Summary" });
    const user = userEvent.setup();
    await user.selectOptions(await summary().findByRole("combobox", { name: "Ship to" }), "AQ");
    await user.click(summary().getByRole("button", { name: "Review order" }));
    expect(await summary().findByRole("alert")).toHaveTextContent("We don’t ship to that destination.");
  });
});
