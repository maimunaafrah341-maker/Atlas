/**
 * Size and colour selection over a product's actual variants. Options come only from the
 * variants the API returned (never from the full vocabulary), and a selection never points at a
 * size/colour combination that doesn't exist.
 */

import type { Variant } from "../../lib/api.ts";
import { COLOURS, SIZES } from "../../lib/catalogue.ts";

export interface Selection {
  size: string | null;
  colour: string | null;
}

/** Distinct values in the vocabulary's display order (unknown values last, as given). */
function ordered(values: string[], vocabulary: readonly string[]): string[] {
  const distinct = [...new Set(values)];
  const rank = (value: string) => {
    const index = vocabulary.indexOf(value);
    return index === -1 ? vocabulary.length : index;
  };
  return distinct.sort((a, b) => rank(a) - rank(b));
}

export function sizesOf(variants: readonly Variant[]): string[] {
  return ordered(
    variants.map((variant) => variant.size),
    SIZES,
  );
}

export function coloursOf(variants: readonly Variant[]): string[] {
  return ordered(
    variants.map((variant) => variant.colour),
    COLOURS,
  );
}

export function findVariant(variants: readonly Variant[], size: string | null, colour: string | null): Variant | null {
  if (size === null || colour === null) return null;
  return variants.find((variant) => variant.size === size && variant.colour === colour) ?? null;
}

/** Sizes offered in a colour (all sizes when no colour is chosen). */
export function sizesIn(variants: readonly Variant[], colour: string | null): string[] {
  return sizesOf(colour === null ? variants : variants.filter((variant) => variant.colour === colour));
}

/** Colours offered in a size (all colours when no size is chosen). */
export function coloursIn(variants: readonly Variant[], size: string | null): string[] {
  return coloursOf(size === null ? variants : variants.filter((variant) => variant.size === size));
}

/**
 * Make a selection consistent: drop values the product doesn't have, drop a size that doesn't
 * exist in the chosen colour, and fill a dimension that has only one possible value.
 */
export function settle(variants: readonly Variant[], selection: Selection): Selection {
  let { size, colour } = selection;
  if (colour !== null && !coloursOf(variants).includes(colour)) colour = null;
  if (size !== null && !sizesIn(variants, colour).includes(size)) size = null;
  const sizes = sizesIn(variants, colour);
  if (size === null && sizes.length === 1) size = sizes[0] ?? null;
  const colours = coloursIn(variants, size);
  if (colour === null && colours.length === 1) colour = colours[0] ?? null;
  return { size, colour };
}

/** Choose a colour; a size that doesn't come in it is cleared rather than silently swapped. */
export function chooseColour(variants: readonly Variant[], selection: Selection, colour: string): Selection {
  return settle(variants, { size: selection.size, colour });
}

/** Choose a size. Sizes missing in the chosen colour are not offered, so this keeps the colour. */
export function chooseSize(variants: readonly Variant[], selection: Selection, size: string): Selection {
  return settle(variants, { size, colour: selection.colour });
}
