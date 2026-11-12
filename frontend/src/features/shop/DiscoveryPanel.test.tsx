import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogueLanguageProvider } from "../../app/catalogueLanguage.tsx";
import { routes } from "../../app/routes.tsx";
import type { Discovery } from "../../lib/api.ts";
import { errorResponse, jsonResponse, page, product, withCart } from "../../test/fixtures.ts";

function renderShop() {
  const router = createMemoryRouter(routes, { initialEntries: ["/shop"] });
  render(
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>,
  );
  return router;
}

function discovery(overrides: Partial<Discovery> = {}): Discovery {
  return {
    source: "gemini",
    model: "gemini-3.8-flash",
    fallback_reason: null,
    intent: { categories: ["jackets"], sizes: ["M"], colours: ["navy"], tags: [], keywords: ["lightweight"] },
    relaxed: [],
    results: page([product({ slug: "ridgeline-rain-shell", name: "Ridgeline Rain Shell" })]),
    ...overrides,
  };
}

/** The catalogue answers with an unrelated product, so discovery results are distinguishable. */
function stub(answer: () => Response | Promise<Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/discovery") return answer();
    void init;
    return jsonResponse(page([product({ slug: "summit-shell", name: "Summit Shell" })]));
  });
  vi.stubGlobal("fetch", withCart(fetchMock));
  return fetchMock;
}

const panel = () => within(screen.getByRole("region", { name: "Describe what you need" }));
const ASK = "I'm looking for a lightweight navy jacket in medium";

async function ask(text = ASK) {
  const user = userEvent.setup();
  await user.type(panel().getByRole("textbox", { name: "Describe what you’re looking for" }), text);
  await user.click(panel().getByRole("button", { name: "Find products" }));
  return user;
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("natural-language discovery", () => {
  it("sends the request and shows Gemini's interpretation with real catalogue products", async () => {
    const fetchMock = stub(() => jsonResponse(discovery()));
    renderShop();
    await ask();
    const matches = await panel().findByRole("list", { name: "Matching products" });
    expect(within(matches).getByRole("link", { name: "Ridgeline Rain Shell" })).toHaveAttribute("href", "/products/ridgeline-rain-shell");
    expect(within(matches).getByRole("article")).toHaveTextContent("$129.00");
    expect(panel().getByText(/Understood by Google Gemini \(gemini-3\.8-flash\)/)).toBeVisible();
    const chips = within(panel().getByRole("list", { name: "Interpreted request" }));
    expect(chips.getAllByRole("listitem").map((chip) => chip.textContent)).toEqual(["Jackets", "Size M", "Navy", "“lightweight”"]);

    const call = fetchMock.mock.calls.find(([url]) => String(url) === "/api/discovery");
    expect(call?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ query: ASK, lang: "en" });
    // The ordinary catalogue is still listed below.
    expect(await screen.findByRole("link", { name: "Summit Shell" })).toBeVisible();
  });

  it("shows a loading state while matching", async () => {
    let release: (response: Response) => void = () => {};
    stub(() => new Promise<Response>((resolve) => (release = resolve)));
    renderShop();
    await ask();
    expect(panel().getByText(`Matching “${ASK}” to the catalogue…`)).toBeVisible();
    expect(panel().getByRole("button", { name: "Finding…" })).toBeDisabled();
    release(jsonResponse(discovery()));
    expect(await panel().findByRole("list", { name: "Matching products" })).toBeVisible();
  });

  it("says plainly when Gemini was unavailable and keyword matching was used", async () => {
    stub(() => jsonResponse(discovery({ source: "keywords", model: null, fallback_reason: "unavailable: timeout" })));
    renderShop();
    await ask();
    expect(await panel().findByText(/Gemini couldn’t answer just now, so your words were matched to the catalogue directly/)).toBeVisible();
    expect(panel().queryByText(/Understood by Google Gemini/)).not.toBeInTheDocument();
  });

  it("says when Gemini isn't configured on the server", async () => {
    stub(() => jsonResponse(discovery({ source: "keywords", model: null, fallback_reason: "not_configured" })));
    renderShop();
    await ask();
    expect(await panel().findByText(/Gemini isn’t configured on this server/)).toBeVisible();
  });

  it("explains relaxed constraints and an empty result", async () => {
    stub(() => jsonResponse(discovery({ relaxed: ["keywords", "tags"], results: page([], { total: 0, total_pages: 0 }) })));
    renderShop();
    await ask();
    expect(await panel().findByText(/the extra words and the features were left out/)).toBeVisible();
    expect(panel().getByText(/No products match that yet/)).toBeVisible();
  });

  it("shows an error with a retry", async () => {
    let calls = 0;
    stub(() => (++calls === 1 ? errorResponse(503, "unavailable", "Service unavailable.") : jsonResponse(discovery())));
    renderShop();
    const user = await ask();
    expect(await panel().findByRole("alert")).toHaveTextContent("The products couldn’t be matched. Service unavailable.");
    await user.click(panel().getByRole("button", { name: "Try again" }));
    expect(await panel().findByRole("list", { name: "Matching products" })).toBeVisible();
  });

  it("rejects an empty request without calling the API", async () => {
    const fetchMock = stub(() => jsonResponse(discovery()));
    renderShop();
    const user = userEvent.setup();
    await user.click(panel().getByRole("button", { name: "Find products" }));
    expect(panel().getByRole("alert")).toHaveTextContent("Describe what you’re looking for in a few words.");
    expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/discovery")).toBe(false);
  });

  it("applies the interpretation as ordinary shop filters", async () => {
    stub(() => jsonResponse(discovery()));
    const router = renderShop();
    const user = await ask();
    await user.click(await panel().findByRole("button", { name: "Apply as shop filters" }));
    const params = new URLSearchParams(router.state.location.search);
    expect(params.getAll("category")).toEqual(["jackets"]);
    expect(params.getAll("size")).toEqual(["M"]);
    expect(params.getAll("colour")).toEqual(["navy"]);
    expect(params.get("q")).toBe("lightweight");
  });

  it("runs an example request with one click, and closes", async () => {
    stub(() => jsonResponse(discovery()));
    renderShop();
    const user = userEvent.setup();
    await user.click(panel().getByRole("button", { name: "A lightweight navy jacket in medium" }));
    expect(await panel().findByRole("list", { name: "Matching products" })).toBeVisible();
    await user.click(panel().getByRole("button", { name: "Close" }));
    expect(panel().queryByRole("list", { name: "Matching products" })).not.toBeInTheDocument();
  });

  it("treats a malformed response as an error", async () => {
    stub(() => jsonResponse({ source: "gemini", intent: {}, results: { items: [{ price: 1 }] } }));
    renderShop();
    await ask();
    expect(await panel().findByRole("alert")).toHaveTextContent("Unexpected discovery response from the server.");
  });
});
