import type { Category } from "../../lib/catalogue.ts";

/** Presentation only: Concept C's restrained tints, one per category, for image placeholders. */
export const CATEGORY_TINTS: Record<Category, string> = {
  jackets: "bg-tint-stone",
  base_layers: "bg-tint-sage",
  trousers: "bg-tint-clay",
  socks: "bg-tint-sand",
  accessories: "bg-tint-moss",
};
