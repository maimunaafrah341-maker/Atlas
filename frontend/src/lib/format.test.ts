import { describe, expect, it } from "vitest";
import { colourLabel, formatMoney, formatPriceRange, productAvailability, sizeLabel } from "./format.ts";

const usd = (amount_minor: number) => ({ amount_minor, currency: "USD" });

describe("formatMoney", () => {
  it.each([
    [12900, "$129.00"],
    [2900, "$29.00"],
    [5, "$0.05"],
    [123456, "$1,234.56"],
    [0, "$0.00"],
  ])("formats %i USD cents as %s", (minor, expected) => {
    expect(formatMoney(usd(minor))).toBe(expected);
  });

  it("uses the currency's own minor-unit digits", () => {
    // JPY has no minor unit: 500 minor units are ¥500, not ¥5.
    expect(formatMoney({ amount_minor: 500, currency: "JPY" })).toBe("¥500");
  });
});

describe("formatPriceRange", () => {
  it("shows one price when every variant costs the same", () => {
    expect(formatPriceRange({ min: usd(12900), max: usd(12900) })).toBe("$129.00");
  });

  it("shows the range when variants differ", () => {
    expect(formatPriceRange({ min: usd(12900), max: usd(14900) })).toBe("$129.00 – $149.00");
  });
});

describe("productAvailability", () => {
  const v = (stock_status: "in_stock" | "low_stock" | "out_of_stock") => ({ stock_status });

  it("is in stock if any variant is", () => {
    expect(productAvailability([v("out_of_stock"), v("low_stock"), v("in_stock")])).toBe("in_stock");
  });

  it("is low stock if the best variant is low", () => {
    expect(productAvailability([v("out_of_stock"), v("low_stock")])).toBe("low_stock");
  });

  it("is out of stock otherwise, including with no variants", () => {
    expect(productAvailability([v("out_of_stock")])).toBe("out_of_stock");
    expect(productAvailability([])).toBe("out_of_stock");
  });
});

describe("labels", () => {
  it("renders size and colour codes for shoppers", () => {
    expect(sizeLabel("S_M")).toBe("S/M");
    expect(sizeLabel("L_XL")).toBe("L/XL");
    expect(sizeLabel("ONE_SIZE")).toBe("One size");
    expect(sizeLabel("XL")).toBe("XL");
    expect(colourLabel("olive")).toBe("Olive");
  });
});
