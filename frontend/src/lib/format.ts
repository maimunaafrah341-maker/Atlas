/**
 * Display formatting. Amounts arrive as integer minor units plus a currency code; they are
 * formatted here with Intl, never by string manipulation, and never rounded or converted.
 * The interface is English until the UI catalogues arrive (M7), so the display locale is "en-US".
 */

import type { Money, StockStatus, Variant } from "./api.ts";

export const DISPLAY_LOCALE = "en-US";

const formatters = new Map<string, Intl.NumberFormat>();

function currencyFormatter(currency: string, locale: string): Intl.NumberFormat {
  const key = `${locale}|${currency}`;
  let formatter = formatters.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, { style: "currency", currency });
    formatters.set(key, formatter);
  }
  return formatter;
}

/** 12900 USD -> "$129.00". The currency's own number of decimal places converts minor units. */
export function formatMoney(money: Money, locale = DISPLAY_LOCALE): string {
  const formatter = currencyFormatter(money.currency, locale);
  const digits = formatter.resolvedOptions().maximumFractionDigits ?? 2;
  return formatter.format(money.amount_minor / 10 ** digits);
}

/** One price, or "$129.00 – $149.00" when variants differ. */
export function formatPriceRange(range: { min: Money; max: Money }, locale = DISPLAY_LOCALE): string {
  const low = formatMoney(range.min, locale);
  const high = formatMoney(range.max, locale);
  return low === high ? low : `${low} – ${high}`;
}

export const STOCK_LABELS: Record<StockStatus, string> = {
  in_stock: "In stock",
  low_stock: "Low stock",
  out_of_stock: "Out of stock",
};

/**
 * A product-level availability summary from its variants' statuses (never counts, D-5):
 * in stock if any variant is, otherwise low stock if any is, otherwise out of stock.
 */
export function productAvailability(variants: readonly Pick<Variant, "stock_status">[]): StockStatus {
  if (variants.some((variant) => variant.stock_status === "in_stock")) return "in_stock";
  if (variants.some((variant) => variant.stock_status === "low_stock")) return "low_stock";
  return "out_of_stock";
}

const SIZE_LABELS: Record<string, string> = { S_M: "S/M", L_XL: "L/XL", ONE_SIZE: "One size" };

/** Size codes as shoppers read them: "S_M" -> "S/M", "ONE_SIZE" -> "One size". */
export function sizeLabel(size: string): string {
  return SIZE_LABELS[size] ?? size;
}

/** Colour codes as words: "olive" -> "Olive". */
export function colourLabel(colour: string): string {
  return colour.charAt(0).toUpperCase() + colour.slice(1);
}
