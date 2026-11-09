import { describe, expect, it } from "vitest";
import { productQueryString } from "../../lib/api.ts";
import { pageItems } from "./ShopControls.tsx";
import {
  NO_FILTERS,
  activeFilterCount,
  changeFilters,
  effectiveSort,
  hasVariantFilters,
  isSearchable,
  parseShopParams,
  shopParams,
  toProductQuery,
} from "./shopQuery.ts";

const parse = (search: string) => parseShopParams(new URLSearchParams(search));

describe("parseShopParams", () => {
  it("reads every supported filter from the URL", () => {
    expect(
      parse("q=rain+shell&category=socks&category=jackets&size=M&colour=navy&min_price=50&max_price=150&in_stock=1&sort=price_desc&page=3"),
    ).toEqual({
      q: "rain shell",
      categories: ["jackets", "socks"],
      sizes: ["M"],
      colours: ["navy"],
      minPrice: 50,
      maxPrice: 150,
      inStock: true,
      sort: "price_desc",
      page: 3,
    });
  });

  it("is unfiltered for an empty URL", () => {
    expect(parse("")).toEqual(NO_FILTERS);
  });

  it("ignores values the API doesn't support instead of sending them", () => {
    expect(
      parse("category=hats&category=jackets&size=XXXL&colour=purple&min_price=-5&max_price=12.50&in_stock=yes&sort=newest&page=0"),
    ).toEqual({ ...NO_FILTERS, categories: ["jackets"] });
    expect(parse("min_price=100001").minPrice).toBeNull(); // above the API's $100,000 limit
    expect(parse("page=abc").page).toBe(1);
  });

  it("puts list values in vocabulary order without duplicates, so equivalent URLs share one request", () => {
    expect(parse("size=XL&size=S&size=XL&size=S_M").sizes).toEqual(["S", "S_M", "XL"]);
  });

  it("drops a relevance sort without a search (the API rejects it)", () => {
    expect(parse("sort=relevance").sort).toBeNull();
    expect(parse("q=wool&sort=relevance").sort).toBe("relevance");
  });

  it("trims the search and caps it at the API's 200 characters", () => {
    expect(parse("q=%20%20wool%20").q).toBe("wool");
    expect(parse(`q=${"a".repeat(250)}`).q).toHaveLength(200);
  });
});

describe("shopParams", () => {
  it("writes only non-default values", () => {
    expect(shopParams(NO_FILTERS).toString()).toBe("");
    expect(shopParams({ ...NO_FILTERS, sort: "featured", page: 1 }).toString()).toBe("");
    expect(shopParams({ ...NO_FILTERS, q: "wool", sort: "relevance" }).toString()).toBe("q=wool");
  });

  it("round-trips through parseShopParams", () => {
    const search = "q=shell&category=jackets&category=socks&size=M&colour=black&min_price=0&max_price=200&in_stock=1&sort=price_asc&page=2";
    expect(shopParams(parse(search)).toString()).toBe(search);
  });
});

describe("changeFilters", () => {
  const onPage3 = { ...NO_FILTERS, page: 3 };

  it("returns to page 1 when any filter changes", () => {
    expect(changeFilters(onPage3, { categories: ["socks"] }).page).toBe(1);
    expect(changeFilters(onPage3, { inStock: true }).page).toBe(1);
    expect(changeFilters(onPage3, { q: "wool" }).page).toBe(1);
    expect(changeFilters(onPage3, { sort: "price_asc" }).page).toBe(1);
  });

  it("drops a relevance sort when the search is cleared", () => {
    expect(changeFilters({ ...NO_FILTERS, q: "wool", sort: "relevance" }, { q: "" }).sort).toBeNull();
  });

  it("stores the default sort as no sort", () => {
    expect(changeFilters(NO_FILTERS, { sort: "featured" }).sort).toBeNull();
    expect(effectiveSort(NO_FILTERS)).toBe("featured");
    expect(effectiveSort({ q: "wool", sort: null })).toBe("relevance");
  });
});

describe("toProductQuery", () => {
  it("builds the API query: dollars become cents, defaults are left to the API", () => {
    const filters = parse("q=shell&category=jackets&size=M&size=L&colour=navy&min_price=50&max_price=150&in_stock=1&sort=price_asc&page=2");
    expect(productQueryString(toProductQuery(filters, "de"))).toBe(
      "q=shell&lang=de&category=jackets&size=M&size=L&colour=navy&min_price_minor=5000&max_price_minor=15000&in_stock=true&sort=price_asc&page=2&page_size=12",
    );
    expect(productQueryString(toProductQuery(NO_FILTERS, "en"))).toBe("lang=en&page=1&page_size=12");
  });

  it("sends a $0 minimum (zero is a value, not 'unset')", () => {
    expect(toProductQuery(parse("min_price=0"), "en").min_price_minor).toBe(0);
  });
});

describe("helpers", () => {
  it("counts narrowing filters, with the price range as one", () => {
    expect(activeFilterCount(parse("category=jackets&size=M&size=L&min_price=1&max_price=2&in_stock=1&q=x"))).toBe(5);
  });

  it("accepts searches the API accepts: a word of two or more letters or digits", () => {
    expect(isSearchable("rain")).toBe(true);
    expect(isSearchable("é")).toBe(false);
    expect(isSearchable("a b c")).toBe(false);
    expect(isSearchable("3l")).toBe(true);
    expect(isSearchable("çà")).toBe(true);
    expect(isSearchable("__ --")).toBe(false);
  });

  it("windows long page lists", () => {
    expect(pageItems(1, 3)).toEqual([1, 2, 3]);
    expect(pageItems(1, 10)).toEqual([1, 2, "gap", 10]);
    expect(pageItems(5, 10)).toEqual([1, "gap", 4, 5, 6, "gap", 10]);
    expect(pageItems(10, 10)).toEqual([1, "gap", 9, 10]);
  });

  it("knows which filters the API applies per variant", () => {
    expect(hasVariantFilters(parse("q=wool&category=socks&sort=price_asc&page=2"))).toBe(false);
    for (const search of ["size=M", "colour=navy", "min_price=0", "max_price=50", "in_stock=1"]) {
      expect(hasVariantFilters(parse(search))).toBe(true);
    }
  });
});
