import { describe, expect, it } from "vitest";
import type { Variant } from "../../lib/api.ts";
import { chooseColour, chooseSize, coloursIn, coloursOf, findVariant, settle, sizesIn, sizesOf } from "./variantSelection.ts";

const v = (size: string, colour: string): Variant => ({
  sku: `X-${size}-${colour}`,
  size,
  colour,
  price: { amount_minor: 1000, currency: "USD" },
  stock_status: "in_stock",
  matches_filters: true,
});

// Navy comes in S and M; red only in M (a partial grid).
const partial = [v("M", "red"), v("S", "navy"), v("M", "navy")];

describe("options", () => {
  it("lists only the product's own sizes and colours, in display order", () => {
    expect(sizesOf([v("XL", "navy"), v("S_M", "navy"), v("S", "navy")])).toEqual(["S", "S_M", "XL"]);
    expect(coloursOf(partial)).toEqual(["navy", "red"]);
    expect(sizesIn(partial, "red")).toEqual(["M"]);
    expect(coloursIn(partial, "S")).toEqual(["navy"]);
    expect(findVariant(partial, "S", "red")).toBeNull();
    expect(findVariant(partial, "S", "navy")?.sku).toBe("X-S-navy");
  });
});

describe("settle", () => {
  it("drops values the product doesn't have", () => {
    expect(settle(partial, { size: "XXL", colour: "purple" })).toEqual({ size: null, colour: null });
  });

  it("drops a size that doesn't come in the chosen colour", () => {
    expect(settle(partial, { size: "S", colour: "red" })).toEqual({ size: "M", colour: "red" });
  });

  it("fills a dimension with only one possible value", () => {
    expect(settle([v("S", "black"), v("M", "black")], { size: null, colour: null })).toEqual({ size: null, colour: "black" });
    expect(settle([v("ONE_SIZE", "navy"), v("ONE_SIZE", "red")], { size: null, colour: null })).toEqual({ size: "ONE_SIZE", colour: null });
    expect(settle(partial, { size: "S", colour: null })).toEqual({ size: "S", colour: "navy" });
  });
});

describe("choosing", () => {
  it("keeps the size when it comes in the new colour", () => {
    expect(chooseColour(partial, { size: "M", colour: "navy" }, "red")).toEqual({ size: "M", colour: "red" });
  });

  it("clears a size that doesn't come in the new colour (here red has one size, so it's filled)", () => {
    expect(chooseColour(partial, { size: "S", colour: "navy" }, "red")).toEqual({ size: "M", colour: "red" });
    const grid = [v("S", "navy"), v("M", "navy"), v("L", "navy"), v("M", "red"), v("L", "red")];
    expect(chooseColour(grid, { size: "S", colour: "navy" }, "red")).toEqual({ size: null, colour: "red" });
  });

  it("chooses a size within the colour", () => {
    expect(chooseSize(partial, { size: null, colour: "navy" }, "S")).toEqual({ size: "S", colour: "navy" });
  });
});
